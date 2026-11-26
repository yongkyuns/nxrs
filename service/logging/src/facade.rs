use log::{Level, LevelFilter, Log, Metadata, Record, SetLoggerError};
use std::fmt::{self, Write};

/// One bounded UTF-8 line. Control characters that would split a line or
/// terminate a C string become spaces.
pub struct Line {
    bytes: [u8; 256],
    len: usize,
}

impl Line {
    fn from_record(record: &Record<'_>) -> Self {
        let mut line = Self {
            bytes: [0; 256],
            len: 0,
        };
        let _ = write!(
            line,
            "[{} {}] {}",
            record.level(),
            record.target(),
            record.args()
        );
        line.bytes[line.len] = b'\n';
        line.len += 1;
        line
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes[..self.len]
    }
}

impl Write for Line {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        for mut ch in text.chars() {
            if ch == '\n' || ch == '\r' || ch == '\0' {
                ch = ' ';
            }
            let size = ch.len_utf8();
            if self.len + size >= self.bytes.len() {
                return Err(fmt::Error);
            }
            ch.encode_utf8(&mut self.bytes[self.len..self.len + size]);
            self.len += size;
        }
        Ok(())
    }
}

struct Backend<F> {
    level: LevelFilter,
    emit: F,
}

impl<F: Fn(Level, Line) + Send + Sync> Log for Backend<F> {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        metadata.level() <= self.level
    }

    fn log(&self, record: &Record<'_>) {
        if self.enabled(record.metadata()) {
            (self.emit)(record.level(), Line::from_record(record));
        }
    }

    fn flush(&self) {}
}

/// Install one backend at startup. Its callback runs on the logging caller.
pub fn install(
    level: LevelFilter,
    emit: impl Fn(Level, Line) + Send + Sync + 'static,
) -> Result<(), SetLoggerError> {
    log::set_boxed_logger(Box::new(Backend { level, emit }))?;
    log::set_max_level(level);
    Ok(())
}

/// Install the standalone command sink using static storage.
pub fn install_stderr(level: LevelFilter) -> Result<(), SetLoggerError> {
    use std::io::Write;
    static LOGGER: Backend<fn(Level, Line)> = Backend {
        level: LevelFilter::Trace,
        emit: |_, line| {
            let _ = std::io::stderr().lock().write_all(line.as_bytes());
        },
    };
    log::set_logger(&LOGGER)?;
    // Initialize the platform stderr lock during setup, before it is needed to
    // report a failed reservation. Some std implementations allocate it lazily.
    let _initialized = std::io::stderr().lock();
    log::set_max_level(level);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formatter_bounds_utf8_and_retains_the_line_ending() {
        let line = Line::from_record(
            &Record::builder()
                .level(log::Level::Warn)
                .target("service")
                .args(format_args!("first\n\0{}", "é".repeat(256)))
                .build(),
        );
        let text = std::str::from_utf8(line.as_bytes()).unwrap();
        assert!(text.len() <= 256);
        assert!(text.starts_with("[WARN service] first  é"));
        assert!(text.ends_with('\n'));
        assert_eq!(text.lines().count(), 1);
    }
}
