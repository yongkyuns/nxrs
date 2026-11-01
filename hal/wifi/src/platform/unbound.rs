use crate::*;
pub(super) struct Unavailable;
impl Wifi for Unavailable {
    fn session(&self) -> SessionId {
        SessionId::new(1).unwrap()
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            scan: false,
            open_network: false,
            wpa2_personal: false,
            credential_storage: CredentialStorage::Volatile,
        }
    }
    fn status(&self) -> ProviderStatus {
        ProviderStatus::Stopped
    }
    fn fault(&self) -> Option<Error> {
        Some(Error::Unsupported)
    }
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Command, Error>> {
        Err(Rejected {
            value: request.command,
            error: Error::Unsupported,
        })
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn finish(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
}
impl QuiescenceControl for Unavailable {
    fn quiesce(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn resume(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
}
