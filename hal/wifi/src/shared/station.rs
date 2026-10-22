//! Private Rust contract for station implementations on the shared worker.
use crate::{AccessPoint, Capabilities, CredentialStorage, Credentials, Error, LinkState, Ssid};
pub(crate) trait Station: Send + 'static + Sized {
    fn open(storage: CredentialStorage) -> Result<Self, Error>;
    fn capabilities(storage: CredentialStorage) -> Capabilities;
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error>;
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error>;
    fn disconnect(&mut self) -> Result<(), Error>;
    fn link(&mut self) -> Result<LinkState, Error>;
    fn close(&mut self) -> Result<(), Error>;
}
