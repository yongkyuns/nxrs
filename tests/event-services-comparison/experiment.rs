#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Layout {
    Lanes,
    Mailbox,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scheduling {
    Event,
    Natural,
    Budget,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Work {
    Monolithic,
    Chunked,
}
#[derive(Clone, Copy, Debug)]
pub struct Experiment {
    pub layout: Layout,
    pub timer_ms: u32,
    pub scheduling: Scheduling,
    pub work: Work,
}
impl Experiment {
    pub fn parse(name: &str) -> Result<Self, String> {
        let parts: Vec<_> = name.split('-').collect();
        if parts.len() != 4 {
            return Err(format!("invalid event experiment: {name}"));
        }
        let layout = match parts[0] {
            "lanes" => Layout::Lanes,
            "mailbox" => Layout::Mailbox,
            _ => return Err("unknown layout".into()),
        };
        let timer_ms = match parts[1] {
            "10ms" => 10,
            "1ms" => 1,
            _ => return Err("unknown timer".into()),
        };
        let scheduling = match parts[2] {
            "event" => Scheduling::Event,
            "natural" => Scheduling::Natural,
            "budget" => Scheduling::Budget,
            _ => return Err("unknown scheduling policy".into()),
        };
        let work = match parts[3] {
            "monolithic" => Work::Monolithic,
            "chunked" if scheduling == Scheduling::Budget => Work::Chunked,
            _ => return Err("chunked work requires budget scheduling".into()),
        };
        Ok(Self {
            layout,
            timer_ms,
            scheduling,
            work,
        })
    }
}
pub fn configure() {
    println!("cargo:rerun-if-env-changed=NXRS_EVENT_CASE");
    let name =
        std::env::var("NXRS_EVENT_CASE").unwrap_or_else(|_| "lanes-10ms-event-monolithic".into());
    let c = Experiment::parse(&name).unwrap_or_else(|error| panic!("{error}"));
    let source = format!(
        "pub const LAYOUT: Layout = Layout::{:?};\npub const TIMER_MS:u32 = {};\npub const SCHEDULING: Scheduling = Scheduling::{:?};\npub const WORK: Work = Work::{:?};\n",
        c.layout, c.timer_ms, c.scheduling, c.work
    );
    std::fs::write(
        std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("event_case.rs"),
        source,
    )
    .unwrap();
}
