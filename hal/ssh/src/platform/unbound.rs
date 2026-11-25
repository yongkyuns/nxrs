use std::io;

pub struct Session;

pub fn start() -> io::Result<Option<Session>> {
    Ok(None)
}

impl Session {
    pub fn stop(self) -> io::Result<()> {
        Ok(())
    }
}
