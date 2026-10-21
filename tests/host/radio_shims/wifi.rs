//! C witness admission only. Production callers use the Rust Station contract.
use crate::{
    behavior::State, AccessPoint, CredentialStorage, Credentials, Error, LinkState, Passphrase,
    Security, Ssid, Station,
};
use core::ffi::c_void;
#[repr(C)]
pub struct Row {
    ssid_length: u32,
    security: u32,
    rssi_dbm: i32,
    ssid: [u8; 32],
}
fn code(result: Result<(), Error>) -> i32 {
    match result {
        Ok(()) => 0,
        Err(e) => match e {
            Error::Unsupported => 1,
            Error::Busy => 2,
            Error::Unavailable => 3,
            Error::PermissionDenied => 4,
            Error::AuthenticationFailed => 5,
            Error::InvalidConfig => 6,
            Error::Timeout => 7,
            Error::Protocol => 8,
            Error::Io => 9,
        },
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_open(out: *mut *mut c_void, storage: u32) -> i32 {
    if out.is_null() {
        return 6;
    }
    unsafe {
        *out = core::ptr::null_mut();
    }
    let storage = match storage {
        0 => CredentialStorage::Volatile,
        1 => CredentialStorage::SystemManaged,
        _ => return 6,
    };
    match State::open(storage) {
        Ok(s) => {
            unsafe {
                *out = std::boxed::Box::into_raw(std::boxed::Box::new(s)).cast();
            }
            0
        }
        Err(e) => code(Err(e)),
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_scan(
    context: *mut c_void,
    rows: *mut Row,
    capacity: u32,
    count: *mut u32,
    truncated: *mut u32,
) -> i32 {
    if context.is_null()
        || rows.is_null()
        || capacity == 0
        || count.is_null()
        || truncated.is_null()
    {
        return 6;
    }
    unsafe {
        *count = 0;
        *truncated = 0;
    }
    let mut n = 0;
    let result = unsafe { &mut *context.cast::<State>() }.scan(&mut |point: AccessPoint| {
        if n == capacity {
            return false;
        }
        let mut row = Row {
            ssid_length: 0,
            security: match point.security {
                Security::Open => 0,
                Security::Wpa2Personal => 1,
                Security::Other => 2,
            },
            rssi_dbm: point.rssi_dbm.map_or(i32::MIN, i32::from),
            ssid: [0; 32],
        };
        if let Some(ssid) = point.ssid {
            row.ssid_length = ssid.as_bytes().len() as u32;
            row.ssid[..ssid.as_bytes().len()].copy_from_slice(ssid.as_bytes());
        }
        unsafe {
            rows.add(n as usize).write(row);
        }
        n += 1;
        true
    });
    match result {
        Ok(complete) => {
            unsafe {
                *count = n;
                *truncated = u32::from(!complete);
            }
            0
        }
        Err(e) => code(Err(e)),
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_connect(
    context: *mut c_void,
    ssid: *const u8,
    len: usize,
    pass: *const u8,
    pass_len: usize,
) -> i32 {
    if context.is_null()
        || ssid.is_null()
        || len == 0
        || len > 32
        || (pass_len != 0 && (pass.is_null() || !(8..=63).contains(&pass_len)))
    {
        return 6;
    }
    let ssid = Ssid::new(unsafe { core::slice::from_raw_parts(ssid, len) }).unwrap();
    let credentials = if pass_len == 0 {
        Credentials::Open
    } else {
        let Ok(text) = std::str::from_utf8(unsafe { core::slice::from_raw_parts(pass, pass_len) })
        else {
            return 6;
        };
        match Passphrase::new(text) {
            Ok(p) => Credentials::Wpa2Personal(p),
            Err(_) => return 6,
        }
    };
    code(unsafe { &mut *context.cast::<State>() }.connect(&ssid, &credentials))
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_link(context: *mut c_void, out: *mut u32) -> i32 {
    if context.is_null() || out.is_null() {
        return 6;
    }
    match unsafe { &mut *context.cast::<State>() }.link() {
        Ok(value) => {
            unsafe {
                *out = match value {
                    LinkState::Unknown => 0,
                    LinkState::Disconnected => 1,
                    LinkState::Associated => 2,
                    LinkState::Addressed => 3,
                };
            }
            0
        }
        Err(e) => code(Err(e)),
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_disconnect(context: *mut c_void) -> i32 {
    if context.is_null() {
        return 6;
    }
    code(unsafe { &mut *context.cast::<State>() }.disconnect())
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_release(context: *mut c_void) -> i32 {
    if context.is_null() {
        return 0;
    }
    let status = code(unsafe { &mut *context.cast::<State>() }.close());
    if status == 0 {
        unsafe {
            drop(std::boxed::Box::from_raw(context.cast::<State>()));
        }
    }
    status
}
