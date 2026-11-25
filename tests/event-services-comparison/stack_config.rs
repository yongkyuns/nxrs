//! Build-only configuration for the event fixture's shared Embassy stack.

pub const DEFAULT_SHARED_STACK_BYTES: usize = 8192;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StackConfig {
    pub bytes: usize,
    pub watermark: bool,
}

impl StackConfig {
    pub fn parse(bytes: Option<&str>, watermark: Option<&str>) -> Result<Self, String> {
        let bytes = match bytes {
            None => DEFAULT_SHARED_STACK_BYTES,
            Some(text) if !text.is_empty() && text.bytes().all(|value| value.is_ascii_digit()) => {
                text.parse::<usize>().map_err(|_| {
                    "NXRS_EVENT_STACK_BYTES must be an unsigned decimal byte count".to_owned()
                })?
            }
            Some(_) => return Err("NXRS_EVENT_STACK_BYTES must be an unsigned decimal byte count".into()),
        };
        if !(1024..=65536).contains(&bytes) || bytes % 16 != 0 {
            return Err("NXRS_EVENT_STACK_BYTES must be 1024..65536 and divisible by 16".into());
        }
        let watermark = match watermark {
            None | Some("0") => false,
            Some("1") => true,
            _ => return Err("NXRS_EVENT_STACK_WATERMARK must be 0 or 1".into()),
        };
        Ok(Self { bytes, watermark })
    }

    pub fn from_environment() -> Self {
        println!("cargo:rerun-if-env-changed=NXRS_EVENT_STACK_BYTES");
        println!("cargo:rerun-if-env-changed=NXRS_EVENT_STACK_WATERMARK");
        let bytes = std::env::var("NXRS_EVENT_STACK_BYTES").ok();
        let watermark = std::env::var("NXRS_EVENT_STACK_WATERMARK").ok();
        Self::parse(bytes.as_deref(), watermark.as_deref())
            .unwrap_or_else(|error| panic!("{error}"))
    }

    pub fn linker_script(self) -> String {
        format!(
            "/* Event fixture only: shared main/executor/interrupt stack. */\n\
             SECTIONS {{\n\
             .stack ORIGIN(RWDATA) + LENGTH(RWDATA) - {bytes} (NOLOAD) : ALIGN(16) {{\n\
             _stack_end = ABSOLUTE(.);\n\
             _stack_end_cpu0 = ABSOLUTE(.);\n\
             __stack_chk_guard = ABSOLUTE(.) + 64;\n\
             . += {bytes};\n\
             _stack_start = ABSOLUTE(.);\n\
             _stack_start_cpu0 = ABSOLUTE(.);\n\
             }} > RWDATA\n\
             }}\n",
            bytes = self.bytes
        )
    }

    pub fn rust_constants(self) -> String {
        format!(
            "pub const SHARED_STACK_BYTES: usize = {};\npub const STACK_WATERMARK: bool = {};\n",
            self.bytes, self.watermark
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_keep_the_historical_stack_and_leave_invasive_observation_off() {
        assert_eq!(
            StackConfig::parse(None, None).unwrap(),
            StackConfig { bytes: 8192, watermark: false }
        );
    }

    #[test]
    fn smaller_stack_changes_both_linker_boundaries_and_runtime_identity() {
        let config = StackConfig::parse(Some("2048"), Some("1")).unwrap();
        let script = config.linker_script();
        assert!(script.contains("LENGTH(RWDATA) - 2048"));
        assert!(script.contains(". += 2048;"));
        assert!(script.contains("__stack_chk_guard = ABSOLUTE(.) + 64;"));
        assert!(config.rust_constants().contains("SHARED_STACK_BYTES: usize = 2048"));
        assert!(config.rust_constants().contains("STACK_WATERMARK: bool = true"));
    }

    #[test]
    fn rejects_undersized_unaligned_and_malformed_controls() {
        for bytes in ["0", "512", "1025", "-1024", "+1024", "65552", "8192junk", "0x2000"] {
            assert!(StackConfig::parse(Some(bytes), None).is_err(), "{bytes}");
        }
        for flag in ["", "yes", "true", "2"] {
            assert!(StackConfig::parse(None, Some(flag)).is_err(), "{flag}");
        }
    }
}
