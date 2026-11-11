//! Preserve acquisition signatures when the profile does not admit BLE.
use crate::{Ble, Capabilities, Error, Event, ProviderStatus, Rejected, Request, SessionId};

pub(super) enum Unavailable {}
impl Ble for Unavailable {
    fn session(&self) -> SessionId {
        match *self {}
    }
    fn capabilities(&self) -> Capabilities {
        match *self {}
    }
    fn status(&self) -> ProviderStatus {
        match *self {}
    }
    fn fault(&self) -> Option<Error> {
        match *self {}
    }
    fn submit(&mut self, _: Request) -> Result<(), Rejected<Request, Error>> {
        match *self {}
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        match *self {}
    }
    fn finish(&mut self) -> Result<(), Error> {
        match *self {}
    }
}
impl<const N: usize> crate::gatt::Driver<N> for Unavailable {
    fn session(&self) -> SessionId {
        match *self {}
    }
    fn submit(
        &mut self,
        _: crate::gatt::Request<N>,
    ) -> Result<(), Rejected<crate::gatt::Request<N>, Error>> {
        match *self {}
    }
    fn recover(&mut self, _: crate::RequestId) {
        match *self {}
    }
    fn poll(&mut self) -> Option<crate::gatt::Event<N>> {
        match *self {}
    }
}
pub(crate) fn open<S>(_: S) -> Result<impl Ble + Send, Error>
where
    S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
{
    Err::<Unavailable, _>(Error::Unsupported)
}
pub(crate) fn gatt_driver<const N: usize>(
    _: impl Fn() + Send + Sync + 'static,
) -> Result<impl crate::gatt::Driver<N>, Error> {
    Err::<Unavailable, _>(Error::Unsupported)
}

#[cfg(test)]
mod tests {
    #[test]
    fn unbound_factories_preserve_portable_callers() {
        assert!(matches!(
            crate::open(|_| Ok(())),
            Err(crate::Error::Unsupported)
        ));
        assert!(matches!(
            crate::gatt::open::<64>(|| {}, 1000),
            Err(crate::Error::Unsupported)
        ));
        assert!(matches!(
            crate::gatt::open::<64>(|| {}, 0),
            Err(crate::Error::InvalidValue)
        ));
    }
}
