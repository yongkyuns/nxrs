//! Private GIO bindings. Native objects stay opaque; owned variants use RAII.
#![allow(dead_code)]
use super::{error_class, error_text, native_constant};
use std::prelude::v1::*;
use std::rc::Rc;
use std::{
    cell::RefCell,
    ffi::{c_char, c_void, CStr, CString},
    ptr::{null, null_mut},
    sync::OnceLock,
    time::{Duration, Instant},
};
pub type G = *mut c_void;
pub type Signal =
    unsafe extern "C" fn(G, *const c_char, *const c_char, *const c_char, *const c_char, G, G);
unsafe extern "C" {
    fn g_error_free(error: G);
    fn g_dbus_error_get_remote_error(error: G) -> *mut c_char;
    fn g_dbus_error_is_remote_error(error: G) -> i32;
    fn g_free(value: G);
    fn g_object_unref(value: G);
    fn g_dbus_address_get_for_bus_sync(kind: i32, cancel: G, error: *mut G) -> *mut c_char;
    fn g_dbus_connection_new_for_address_sync(
        address: *const c_char,
        flags: i32,
        observer: G,
        cancel: G,
        error: *mut G,
    ) -> G;
    fn g_dbus_connection_set_exit_on_close(bus: G, exit: i32);
    fn g_dbus_connection_close_sync(bus: G, cancel: G, error: *mut G) -> i32;
    pub fn g_dbus_connection_is_closed(bus: G) -> i32;
    fn g_dbus_connection_call_sync(
        bus: G,
        name: *const c_char,
        path: *const c_char,
        interface: *const c_char,
        method: *const c_char,
        parameters: G,
        reply_type: *const c_char,
        flags: i32,
        timeout: i32,
        cancel: G,
        error: *mut G,
    ) -> G;
    fn g_dbus_connection_call(
        bus: G,
        name: *const c_char,
        path: *const c_char,
        interface: *const c_char,
        method: *const c_char,
        parameters: G,
        reply_type: *const c_char,
        flags: i32,
        timeout: i32,
        cancel: G,
        callback: unsafe extern "C" fn(G, G, G),
        data: G,
    );
    fn g_dbus_connection_call_finish(bus: G, result: G, error: *mut G) -> G;
    fn g_dbus_connection_signal_subscribe(
        bus: G,
        name: *const c_char,
        interface: *const c_char,
        signal: *const c_char,
        path: *const c_char,
        arg: *const c_char,
        flags: i32,
        callback: Signal,
        data: G,
        destroy: unsafe extern "C" fn(G),
    ) -> u32;
    fn g_dbus_connection_signal_unsubscribe(bus: G, id: u32);
    pub fn g_main_context_new() -> G;
    fn g_main_context_ref(context: G) -> G;
    pub fn g_main_context_unref(context: G);
    pub fn g_main_context_push_thread_default(context: G);
    pub fn g_main_context_pop_thread_default(context: G);
    pub fn g_main_context_iteration(context: G, blocking: i32) -> i32;
    pub fn g_main_context_pending(context: G) -> i32;
    pub fn g_main_context_wakeup(context: G);
    pub fn g_timeout_source_new(ms: u32) -> G;
    pub fn g_source_set_callback(
        source: G,
        callback: unsafe extern "C" fn(G) -> i32,
        data: G,
        destroy: Option<unsafe extern "C" fn(G)>,
    );
    pub fn g_source_attach(source: G, context: G) -> u32;
    pub fn g_source_destroy(source: G);
    pub fn g_source_unref(source: G);
    fn g_variant_ref_sink(value: G) -> G;
    fn g_variant_ref(value: G) -> G;
    fn g_variant_unref(value: G);
    fn g_variant_get_child_value(value: G, index: usize) -> G;
    fn g_variant_n_children(value: G) -> usize;
    fn g_variant_lookup_value(value: G, key: *const c_char, kind: *const c_char) -> G;
    fn g_variant_is_of_type(value: G, kind: *const c_char) -> i32;
    fn g_variant_get_string(value: G, length: *mut usize) -> *const c_char;
    fn g_variant_get_boolean(value: G) -> i32;
    fn g_variant_get_uint32(value: G) -> u32;
    fn g_variant_get_int16(value: G) -> i16;
    fn g_variant_get_int64(value: G) -> i64;
    fn g_variant_get_fixed_array(value: G, len: *mut usize, size: usize) -> *const u8;
    fn g_variant_new_string(value: *const c_char) -> G;
    fn g_variant_new_object_path(value: *const c_char) -> G;
    fn g_variant_new_boolean(value: i32) -> G;
    fn g_variant_new_uint32(value: u32) -> G;
    fn g_variant_new_variant(value: G) -> G;
    fn g_variant_new_fixed_array(
        kind: *const c_char,
        bytes: *const u8,
        len: usize,
        size: usize,
    ) -> G;
    fn g_variant_new_tuple(values: *const G, len: usize) -> G;
    fn g_variant_new_dict_entry(key: G, value: G) -> G;
    fn g_variant_new_array(kind: *const c_char, values: *const G, len: usize) -> G;
    fn g_uuid_string_random() -> *mut c_char;
    fn g_checksum_new(kind: i32) -> G;
    fn g_checksum_update(checksum: G, data: *const u8, len: isize);
    fn g_checksum_get_digest(checksum: G, data: *mut u8, len: *mut usize);
    fn g_checksum_free(checksum: G);
}
pub fn constant(kind: u32) -> i32 {
    unsafe { native_constant(kind) }
}
pub fn c(value: &str) -> CString {
    CString::new(value).expect("private D-Bus string contains NUL")
}
unsafe fn text(value: *const c_char) -> String {
    if value.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(value) }
            .to_string_lossy()
            .into_owned()
    }
}
fn owned_text(value: *mut c_char) -> String {
    let result = unsafe { text(value) };
    unsafe { g_free(value.cast()) };
    result
}
#[derive(Debug, Clone)]
pub struct Failure {
    pub class: u32,
    pub name: String,
    pub message: String,
    pub remote: bool,
}
impl Failure {
    fn take(error: G) -> Self {
        if error.is_null() {
            return Self {
                class: 0,
                name: String::new(),
                message: String::new(),
                remote: false,
            };
        }
        let out = Self {
            class: unsafe { error_class(error) },
            name: owned_text(unsafe { g_dbus_error_get_remote_error(error) }),
            message: owned_text(unsafe { error_text(error) }),
            remote: unsafe { g_dbus_error_is_remote_error(error) } != 0,
        };
        unsafe { g_error_free(error) };
        out
    }
}
pub struct V(pub G);
impl Drop for V {
    fn drop(&mut self) {
        unsafe { g_variant_unref(self.0) }
    }
}
impl Clone for V {
    fn clone(&self) -> Self {
        Self(unsafe { g_variant_ref(self.0) })
    }
}
impl V {
    fn floating(value: G) -> Self {
        Self(unsafe { g_variant_ref_sink(value) })
    }
    pub fn is(&self, kind: &str) -> bool {
        (unsafe { g_variant_is_of_type(self.0, c(kind).as_ptr()) }) != 0
    }
    pub fn len(&self) -> usize {
        unsafe { g_variant_n_children(self.0) }
    }
    pub fn child(&self, index: usize) -> Self {
        assert!(index < self.len());
        Self(unsafe { g_variant_get_child_value(self.0, index) })
    }
    pub fn lookup(&self, key: &str, kind: &str) -> Option<Self> {
        let p = unsafe { g_variant_lookup_value(self.0, c(key).as_ptr(), c(kind).as_ptr()) };
        (!p.is_null()).then(|| Self(p))
    }
    pub fn has(&self, key: &str) -> bool {
        let p = unsafe { g_variant_lookup_value(self.0, c(key).as_ptr(), null()) };
        if p.is_null() {
            false
        } else {
            drop(Self(p));
            true
        }
    }
    pub fn string(&self) -> String {
        unsafe { text(g_variant_get_string(self.0, null_mut())) }
    }
    pub fn boolean(&self) -> bool {
        (unsafe { g_variant_get_boolean(self.0) }) != 0
    }
    pub fn u32(&self) -> u32 {
        unsafe { g_variant_get_uint32(self.0) }
    }
    pub fn i16(&self) -> i16 {
        unsafe { g_variant_get_int16(self.0) }
    }
    pub fn i64(&self) -> i64 {
        unsafe { g_variant_get_int64(self.0) }
    }
    pub fn bytes(&self) -> Vec<u8> {
        let mut len = 0;
        let p = unsafe { g_variant_get_fixed_array(self.0, &mut len, 1) };
        if len == 0 {
            Vec::new()
        } else {
            unsafe { std::slice::from_raw_parts(p, len) }.to_vec()
        }
    }
    pub fn str(value: &str) -> Self {
        Self::floating(unsafe { g_variant_new_string(c(value).as_ptr()) })
    }
    pub fn path(value: &str) -> Self {
        Self::floating(unsafe { g_variant_new_object_path(c(value).as_ptr()) })
    }
    pub fn bool(value: bool) -> Self {
        Self::floating(unsafe { g_variant_new_boolean(i32::from(value)) })
    }
    pub fn uint(value: u32) -> Self {
        Self::floating(unsafe { g_variant_new_uint32(value) })
    }
    pub fn byte_array(value: &[u8]) -> Self {
        Self::floating(unsafe {
            g_variant_new_fixed_array(c("y").as_ptr(), value.as_ptr(), value.len(), 1)
        })
    }
    pub fn tuple(values: &[Self]) -> Self {
        let raw: Vec<_> = values.iter().map(|v| v.0).collect();
        Self::floating(unsafe { g_variant_new_tuple(raw.as_ptr(), raw.len()) })
    }
    pub fn array(kind: &str, values: &[Self]) -> Self {
        let raw: Vec<_> = values.iter().map(|v| v.0).collect();
        Self::floating(unsafe { g_variant_new_array(c(kind).as_ptr(), raw.as_ptr(), raw.len()) })
    }
    pub fn entry(key: Self, value: Self) -> Self {
        Self::floating(unsafe { g_variant_new_dict_entry(key.0, value.0) })
    }
    pub fn dict(values: Vec<(&str, Self)>) -> Self {
        Self::array(
            "{sv}",
            &values
                .into_iter()
                .map(|(key, value)| {
                    Self::entry(
                        Self::str(key),
                        Self::floating(unsafe { g_variant_new_variant(value.0) }),
                    )
                })
                .collect::<Vec<_>>(),
        )
    }
    pub fn strings(values: &[&str]) -> Self {
        Self::array(
            "s",
            &values.iter().map(|v| Self::str(v)).collect::<Vec<_>>(),
        )
    }
}
pub fn random_uuid() -> String {
    owned_text(unsafe { g_uuid_string_random() })
}
pub fn checksum(parts: &[&[u8]]) -> [u8; 32] {
    unsafe {
        let digest = g_checksum_new(constant(3));
        for part in parts {
            g_checksum_update(digest, part.as_ptr(), part.len() as isize);
        }
        let mut out = [0; 32];
        let mut n = 32;
        g_checksum_get_digest(digest, out.as_mut_ptr(), &mut n);
        g_checksum_free(digest);
        out
    }
}
pub struct Context(pub G);
impl Context {
    pub fn retain(context: G) -> Self {
        Self(unsafe { g_main_context_ref(context) })
    }
    pub fn new() -> Self {
        Self(unsafe { g_main_context_new() })
    }
    pub fn push(&self) {
        unsafe { g_main_context_push_thread_default(self.0) }
    }
    pub fn pop(&self) {
        unsafe { g_main_context_pop_thread_default(self.0) }
    }
    pub fn iterate(&self, blocking: bool) {
        unsafe { g_main_context_iteration(self.0, i32::from(blocking)) };
    }
    pub fn pump(&self, max: usize) {
        for _ in 0..max {
            if unsafe { g_main_context_pending(self.0) } == 0 {
                break;
            }
            self.iterate(false);
        }
    }
}
impl Drop for Context {
    fn drop(&mut self) {
        unsafe { g_main_context_unref(self.0) }
    }
}
pub struct Bus(pub G);
impl Bus {
    pub fn open() -> Result<Self, Failure> {
        let mut error = null_mut();
        let addr = unsafe { g_dbus_address_get_for_bus_sync(constant(0), null_mut(), &mut error) };
        if addr.is_null() {
            return Err(Failure::take(error));
        }
        let bus = unsafe {
            g_dbus_connection_new_for_address_sync(
                addr,
                constant(1),
                null_mut(),
                null_mut(),
                &mut error,
            )
        };
        unsafe { g_free(addr.cast()) };
        if bus.is_null() {
            return Err(Failure::take(error));
        }
        unsafe { g_dbus_connection_set_exit_on_close(bus, 0) };
        Ok(Self(bus))
    }
    pub fn closed(&self) -> bool {
        (unsafe { g_dbus_connection_is_closed(self.0) }) != 0
    }
    #[allow(clippy::too_many_arguments)] // Mirrors the native D-Bus operation.
    pub fn call(
        &self,
        name: &str,
        path: &str,
        interface: &str,
        method: &str,
        parameters: Option<V>,
        signature: &str,
        timeout: i32,
    ) -> Result<V, Failure> {
        let mut error = null_mut();
        let reply = unsafe {
            g_dbus_connection_call_sync(
                self.0,
                c(name).as_ptr(),
                c(path).as_ptr(),
                c(interface).as_ptr(),
                c(method).as_ptr(),
                parameters.as_ref().map_or(null_mut(), |v| v.0),
                c(signature).as_ptr(),
                constant(2),
                timeout,
                null_mut(),
                &mut error,
            )
        };
        if reply.is_null() {
            Err(Failure::take(error))
        } else {
            Ok(V(reply))
        }
    }
    #[allow(clippy::too_many_arguments)] // Mirrors the native asynchronous operation.
    pub fn start(
        &self,
        name: &str,
        path: &str,
        interface: &str,
        method: &str,
        parameters: Option<V>,
        signature: &str,
        timeout: i32,
        context: &Context,
    ) -> Rc<Reply> {
        // Rc keeps a shared allocation stable while GIO holds its callback pointer.
        let reply = Rc::new(Reply(RefCell::new(None)));
        context.push();
        unsafe {
            g_dbus_connection_call(
                self.0,
                c(name).as_ptr(),
                c(path).as_ptr(),
                c(interface).as_ptr(),
                c(method).as_ptr(),
                parameters.as_ref().map_or(null_mut(), |v| v.0),
                c(signature).as_ptr(),
                constant(2),
                timeout,
                null_mut(),
                replied,
                Rc::as_ptr(&reply).cast_mut().cast(),
            )
        };
        context.pop();
        reply
    }
    #[allow(clippy::too_many_arguments)] // Mirrors the native subscription and lifetime hooks.
    pub fn subscribe(
        &self,
        name: &str,
        interface: &str,
        signal: &str,
        path: Option<&str>,
        arg: Option<&str>,
        callback: Signal,
        data: G,
        destroy: unsafe extern "C" fn(G),
        context: &Context,
    ) -> u32 {
        let path = path.map(c);
        let arg = arg.map(c);
        context.push();
        let id = unsafe {
            g_dbus_connection_signal_subscribe(
                self.0,
                c(name).as_ptr(),
                c(interface).as_ptr(),
                c(signal).as_ptr(),
                path.as_ref().map_or(null(), |s| s.as_ptr()),
                arg.as_ref().map_or(null(), |s| s.as_ptr()),
                0,
                callback,
                data,
                destroy,
            )
        };
        context.pop();
        id
    }
    pub fn unsubscribe(&self, id: u32) {
        unsafe { g_dbus_connection_signal_unsubscribe(self.0, id) }
    }
}
impl Drop for Bus {
    fn drop(&mut self) {
        unsafe {
            g_dbus_connection_close_sync(self.0, null_mut(), null_mut());
            g_object_unref(self.0)
        }
    }
}
pub struct Reply(RefCell<Option<Result<V, Failure>>>);
impl Reply {
    pub fn done(&self) -> bool {
        self.0.borrow().is_some()
    }
    pub fn take(&self) -> Option<Result<V, Failure>> {
        self.0.borrow_mut().take()
    }
}
unsafe extern "C" fn replied(bus: G, result: G, data: G) {
    let mut error = null_mut();
    let v = unsafe { g_dbus_connection_call_finish(bus, result, &mut error) };
    let reply = if v.is_null() {
        Err(Failure::take(error))
    } else {
        Ok(V(v))
    };
    unsafe { &*data.cast::<Reply>() }.0.replace(Some(reply));
}
pub unsafe fn borrowed_variant(value: G) -> V {
    V(unsafe { g_variant_ref(value) })
}
pub unsafe fn borrowed_text(value: *const c_char) -> String {
    unsafe { text(value) }
}
static EPOCH: OnceLock<Instant> = OnceLock::new();
pub fn now() -> i64 {
    let micros = EPOCH
        .get_or_init(Instant::now)
        .elapsed()
        .as_micros()
        .min(i64::MAX as u128) as i64;
    #[cfg(nxrs_native_witness)]
    {
        unsafe extern "C" {
            fn nxrs_radio_test_clock(real: i64) -> i64;
        }
        unsafe { nxrs_radio_test_clock(micros) }
    }
    #[cfg(not(nxrs_native_witness))]
    {
        micros
    }
}
pub fn pause(ms: u64) {
    #[cfg(nxrs_native_witness)]
    let ms = {
        unsafe extern "C" {
            fn nxrs_radio_test_fast() -> bool;
        }
        if unsafe { nxrs_radio_test_fast() } {
            ms.min(1)
        } else {
            ms
        }
    };
    std::thread::sleep(Duration::from_millis(ms));
}
