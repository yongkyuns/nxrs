//! Typed bridge to the selected desktop SDK; no native structures in the API.
#![allow(unsafe_code)]

use super::Row;
use crate::{Capabilities, CredentialStorage, Credentials, Error, LinkState, Ssid};
use core::{ffi::c_void, ptr::NonNull};

unsafe extern "C" {
    fn nxrs_host_wifi_open(context: *mut *mut c_void, storage: u32) -> i32;
    fn nxrs_host_wifi_scan(
        context: *mut c_void,
        rows: *mut Row,
        capacity: u32,
        count: *mut u32,
        truncated: *mut u32,
    ) -> i32;
    fn nxrs_host_wifi_connect(
        context: *mut c_void,
        ssid: *const u8,
        ssid_length: usize,
        password: *const u8,
        password_length: usize,
    ) -> i32;
    fn nxrs_host_wifi_disconnect(context: *mut c_void) -> i32;
    fn nxrs_host_wifi_link(context: *mut c_void, state: *mut u32) -> i32;
    fn nxrs_host_wifi_release(context: *mut c_void) -> i32;
}

pub(super) struct Bridge {
    context: Option<NonNull<c_void>>,
    storage: CredentialStorage,
    capabilities: Capabilities,
}
// SAFETY: the synchronous bridge has no callbacks/run-loop-bound objects. The
// private worker becomes its sole caller; no method can run concurrently.
unsafe impl Send for Bridge {}

impl Bridge {
    pub(super) fn new(storage: CredentialStorage, capabilities: Capabilities) -> Self {
        Self {
            context: None,
            storage,
            capabilities,
        }
    }
    fn context(&self) -> Result<*mut c_void, Error> {
        self.context.map(NonNull::as_ptr).ok_or(Error::Unavailable)
    }
}

impl super::Bridge for Bridge {
    fn check(&mut self) -> Result<(), Error> {
        if self.context.is_none() {
            let mut context = core::ptr::null_mut();
            let storage = match self.storage {
                CredentialStorage::Volatile => 0,
                CredentialStorage::SystemManaged => 1,
            };
            // SAFETY: worker-owned acquisition, aligned output. No SDK or IPC
            // initialization runs on the service owner's event-loop thread.
            status(unsafe { nxrs_host_wifi_open(&mut context, storage) })?;
            self.context = Some(NonNull::new(context).ok_or(Error::Protocol)?);
        }
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        self.capabilities
    }

    fn scan(&mut self, rows: &mut [Row]) -> Result<(usize, bool), Error> {
        let capacity = u32::try_from(rows.len()).map_err(|_| Error::InvalidConfig)?;
        let (mut count, mut truncated) = (0, 0);
        // SAFETY: writable bounded rows and outputs live through the synchronous call.
        status(unsafe {
            nxrs_host_wifi_scan(
                self.context()?,
                rows.as_mut_ptr(),
                capacity,
                &mut count,
                &mut truncated,
            )
        })?;
        if count > capacity || truncated > 1 {
            return Err(Error::Protocol);
        }
        Ok((count as usize, truncated == 1))
    }
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        if !self.capabilities().open_network {
            return Err(Error::Unsupported);
        }
        let password = match credentials {
            Credentials::Open => &[][..],
            Credentials::Wpa2Personal(value) => value.as_bytes(),
        };
        // SAFETY: borrowed byte slices are retained only during the call. No
        // credential is converted to argv or a log record. OS persistence policy
        // is enforced by the selected native implementation.
        status(unsafe {
            nxrs_host_wifi_connect(
                self.context()?,
                ssid.as_bytes().as_ptr(),
                ssid.as_bytes().len(),
                password.as_ptr(),
                password.len(),
            )
        })
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        // SAFETY: live uniquely owned context, synchronous operation.
        status(unsafe { nxrs_host_wifi_disconnect(self.context()?) })
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        let mut state = 0;
        // SAFETY: valid context and aligned scalar output, no retained pointer.
        status(unsafe { nxrs_host_wifi_link(self.context()?, &mut state) })?;
        match state {
            1 => Ok(LinkState::Disconnected),
            2 => Ok(LinkState::Associated),
            3 => Ok(LinkState::Addressed),
            _ => Err(Error::Protocol),
        }
    }
    fn close(&mut self) -> Result<(), Error> {
        if let Some(context) = self.context {
            // SAFETY: worker-only call. On failure the native context retains
            // any pending callbacks and owned association; do not release the
            // worker's interface lease or claim quiescence yet.
            status(unsafe { nxrs_host_wifi_release(context.as_ptr()) })?;
            self.context = None;
        }
        Ok(())
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = <Self as super::Bridge>::close(self);
    }
}

fn status(value: i32) -> Result<(), Error> {
    match value {
        0 => Ok(()),
        1 => Err(Error::Unsupported),
        2 => Err(Error::Busy),
        3 => Err(Error::Unavailable),
        4 => Err(Error::PermissionDenied),
        5 => Err(Error::AuthenticationFailed),
        6 => Err(Error::InvalidConfig),
        7 => Err(Error::Timeout),
        8 => Err(Error::Protocol),
        9 => Err(Error::Io),
        _ => Err(Error::Protocol),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn injected_capabilities_control_join_without_consulting_platform_identity() {
        use crate::providers::native::Bridge as _;
        for allowed in [false, true] {
            let capabilities = Capabilities {
                scan: true,
                open_network: allowed,
                wpa2_personal: allowed,
                credential_storage: CredentialStorage::SystemManaged,
            };
            let mut bridge = Bridge::new(CredentialStorage::Volatile, capabilities);
            assert_eq!(bridge.capabilities(), capabilities);
            assert_eq!(
                bridge.connect(&Ssid::new(b"fixture").unwrap(), &Credentials::Open),
                Err(if allowed {
                    Error::Unavailable
                } else {
                    Error::Unsupported
                })
            );
        }
    }

    #[test]
    fn every_bridge_status_is_normalized_without_errno_assumptions() {
        let errors = [
            Error::Unsupported,
            Error::Busy,
            Error::Unavailable,
            Error::PermissionDenied,
            Error::AuthenticationFailed,
            Error::InvalidConfig,
            Error::Timeout,
            Error::Protocol,
            Error::Io,
        ];
        assert_eq!(status(0), Ok(()));
        for (index, error) in errors.into_iter().enumerate() {
            assert_eq!(status(index as i32 + 1), Err(error));
        }
        assert_eq!(status(-1), Err(Error::Protocol));
        assert_eq!(status(10), Err(Error::Protocol));
    }
}
