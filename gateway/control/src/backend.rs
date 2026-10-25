//! RTSP authorization ownership, distinct from WebRTC playback grants.
use crate::{config::identifier, policy::MediaAuth, security::{session_id, MAX_SESSIONS}};
use serde::Deserialize;
use std::{collections::{BTreeSet, HashMap, HashSet}, time::{Duration, Instant}};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BackendRole { Publisher, Recorder }
impl BackendRole {
    pub(crate) fn action(self) -> &'static str {
        match self { Self::Publisher => "publish", Self::Recorder => "read" }
    }
}

#[derive(Deserialize)]
pub struct RemoteRtspSession {
    pub id: String,
    pub state: String,
    pub path: String,
    pub user: String,
}
struct Authorization {
    principal: String,
    role: BackendRole,
    path: String,
    admitted: Instant,
    observed: bool,
    invalid: bool,
}
#[derive(Default)]
pub(crate) struct Authorizations { bindings: HashMap<String, Authorization> }
impl Authorizations {
    pub fn len(&self) -> usize { self.bindings.len() }
    pub fn count(&self, principal: &str) -> usize {
        self.bindings.values().filter(|s| s.principal == principal).count()
    }
    pub fn admit(&mut self, request: &MediaAuth, id: Option<&str>, role: BackendRole,
                 now: Instant, other_slots: usize) -> bool {
        let Some(id) = id.filter(|id| session_id(id)) else { return false; };
        if let Some(existing) = self.bindings.get_mut(id) {
            if existing.principal != request.user || existing.path != request.path || existing.role != role {
                existing.invalid = true;
            }
            return !existing.invalid;
        }
        if self.bindings.len().saturating_add(other_slots) >= MAX_SESSIONS { return false; }
        self.bindings.insert(id.to_owned(), Authorization { principal:request.user.clone(), role,
            path:request.path.clone(), admitted:now, observed:false, invalid:false });
        true
    }
    pub fn plan(&mut self, remote: &[RemoteRtspSession], denied: &BTreeSet<String>,
                failed: bool, now: Instant) -> Vec<String> {
        let present: HashSet<_> = remote.iter().map(|s| s.id.as_str()).collect();
        // MediaMTX uses a connection UUID at DESCRIBE and a session UUID at
        // SETUP. Unobserved connection/admission bindings have a finite grace;
        // an observed session is released on its first absent snapshot.
        self.bindings.retain(|id, s| present.contains(id.as_str()) ||
            (!s.observed && now.saturating_duration_since(s.admitted) < Duration::from_secs(30)));
        for remote in remote {
            if let Some(owned) = self.bindings.get_mut(&remote.id) {
                owned.observed = true;
                // Authorization completes before the SDK publishes state/user.
                // An older idle snapshot must not poison a new valid binding.
                if remote.state != "idle" && (remote.state != owned.role.action() ||
                    remote.path != owned.path || remote.user != owned.principal) {
                    owned.invalid = true;
                }
            }
        }
        remote.iter().filter(|s| {
            if self.bindings.contains_key(&s.id) {
                self.forbidden(&s.id, denied, failed)
            } else {
                // An idle session carries no media. Active sessions without a
                // matching authorization are orphans, including after restart.
                s.state != "idle"
            }
        }).map(|s| s.id.clone()).collect()
    }
    pub fn forbidden(&self, id: &str, denied: &BTreeSet<String>, failed: bool) -> bool {
        self.bindings.get(id).is_none_or(|s| failed || s.invalid || denied.contains(&s.principal))
    }
    pub fn retired(&mut self, id: &str) { self.bindings.remove(id); }
}
pub(crate) fn valid_remote(s: &RemoteRtspSession) -> bool {
    session_id(&s.id) && match s.state.as_str() {
        "idle" => (s.user.is_empty() || identifier(&s.user)) && (s.path.is_empty() || identifier(&s.path)),
        "read" | "publish" => identifier(&s.user) && identifier(&s.path),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn id(n: usize) -> String { format!("00000000-0000-4000-8000-{n:012x}") }
    fn request(user: &str, action: &str, path: &str) -> MediaAuth {
        serde_json::from_value(json!({"user":user,"action":action,"path":path,"protocol":"rtsp"})).unwrap()
    }
    fn remote(n: usize, state: &str, user: &str, path: &str) -> RemoteRtspSession {
        RemoteRtspSession { id:id(n),state:state.into(),user:user.into(),path:path.into() }
    }
    #[test]
    fn ownership_changes_are_sticky_and_duplicate_authorizations_keep_their_slot() {
        let now=Instant::now();let mut owner=Authorizations::default();
        let a=request("publisher","publish","one");
        assert!(!owner.admit(&a,None,BackendRole::Publisher,now,0));
        assert!(!owner.admit(&a,Some("bad"),BackendRole::Publisher,now,0));
        assert!(owner.admit(&a,Some(&id(1)),BackendRole::Publisher,now,0));
        assert!(owner.admit(&a,Some(&id(1)),BackendRole::Publisher,now,MAX_SESSIONS));
        assert!(!owner.admit(&request("other","publish","one"),Some(&id(1)),BackendRole::Publisher,now,0));
        assert!(!owner.admit(&a,Some(&id(1)),BackendRole::Publisher,now,0));
        assert!(owner.forbidden(&id(1),&BTreeSet::new(),false));
    }
    #[test]
    fn admission_and_snapshot_races_preserve_new_ownership_but_never_revoked_ownership() {
        let now=Instant::now();let mut owner=Authorizations::default();let mut denied=BTreeSet::new();
        let active=[remote(1,"read","recorder","one")];
        assert_eq!(owner.plan(&active,&denied,false,now),vec![id(1)]);
        assert!(owner.admit(&request("recorder","read","one"),Some(&id(1)),BackendRole::Recorder,now,0));
        assert!(!owner.forbidden(&id(1),&denied,false));
        assert!(owner.plan(&[remote(1,"idle","","")],&denied,false,now).is_empty());
        assert!(owner.plan(&[],&denied,false,now).is_empty());
        // An observed session disappears immediately; a fresh authorization
        // in another identifier retains its negotiation grace.
        assert!(owner.admit(&request("recorder","read","one"),Some(&id(2)),BackendRole::Recorder,now,0));
        owner.plan(&[],&denied,false,now);
        assert_eq!(owner.count("recorder"),1);
        denied.insert("recorder".into());
        assert_eq!(owner.plan(&[remote(2,"read","recorder","one")],&denied,false,now),vec![id(2)]);
        owner.retired(&id(2));assert_eq!(owner.count("recorder"),0);
    }
    #[test]
    fn mismatched_active_metadata_stays_forbidden_and_storage_failure_selects_every_owner() {
        let now=Instant::now();let denied=BTreeSet::new();
        for bad in [remote(1,"read","publisher","one"),remote(1,"publish","other","one"),remote(1,"publish","publisher","other")] {
            let mut owner=Authorizations::default();
            assert!(owner.admit(&request("publisher","publish","one"),Some(&id(1)),BackendRole::Publisher,now,0));
            assert_eq!(owner.plan(&[bad],&denied,false,now),vec![id(1)]);
            assert_eq!(owner.plan(&[remote(1,"publish","publisher","one")],&denied,false,now),vec![id(1)]);
        }
        let mut owner=Authorizations::default();
        assert!(owner.admit(&request("recorder","read","one"),Some(&id(1)),BackendRole::Recorder,now,0));
        assert_eq!(owner.plan(&[remote(1,"read","recorder","one")],&denied,true,now),vec![id(1)]);
    }
    #[test]
    fn connection_and_session_authorizations_are_bounded_and_unobserved_bindings_expire() {
        let now=Instant::now();let mut owner=Authorizations::default();let a=request("recorder","read","one");
        for n in 0..MAX_SESSIONS-1 { assert!(owner.admit(&a,Some(&id(n)),BackendRole::Recorder,now,1)); }
        assert!(!owner.admit(&a,Some(&id(MAX_SESSIONS)),BackendRole::Recorder,now,1));
        owner.plan(&[],&BTreeSet::new(),false,now+Duration::from_secs(30));
        assert_eq!(owner.len(),0);
        assert!(owner.admit(&a,Some(&id(0)),BackendRole::Recorder,now,0));
        assert!(!owner.admit(&a,Some(&id(1)),BackendRole::Recorder,now,usize::MAX));
    }
    #[test]
    fn idle_metadata_has_no_media_but_active_metadata_requires_an_identity_and_path() {
        assert!(valid_remote(&remote(1,"idle","","")));
        assert!(valid_remote(&remote(1,"read","recorder","one")));
        for bad in [remote(1,"read","","one"),remote(1,"publish","publisher","../one"),
                    remote(1,"other","publisher","one")] { assert!(!valid_remote(&bad)); }
        let mut owner=Authorizations::default();
        assert!(owner.plan(&[remote(1,"idle","","")],&BTreeSet::new(),false,Instant::now()).is_empty());
    }
}
