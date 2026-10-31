//! Trusted-USB qualification only; never linked into the ordinary camera binary.
#[cfg(target_os = "nuttx")]
#[path = "../main.rs"]
mod camera;

#[cfg(any(target_os = "nuttx", test))]
mod update {
    use std::io::{Read, Write};
    use std::time::{Duration, Instant};

    unsafe extern "C" {
        fn nxrs_stream_console_ready() -> i32;
    }
    struct Input {
        file: std::fs::File,
        deadline: Instant,
        length: u32,
        offset: u32,
    }
    impl Input {
        fn exact(&mut self, bytes: &mut [u8]) -> std::io::Result<()> {
            let mut offset = 0;
            let deadline = self.deadline.min(Instant::now() + Duration::from_secs(10));
            while offset < bytes.len() {
                if Instant::now() >= deadline {
                    return Err(std::io::ErrorKind::TimedOut.into());
                }
                // SAFETY: existing mechanical fixture ABI polls this stdin.
                match unsafe { nxrs_stream_console_ready() } {
                    n if n < 0 => return Err(std::io::ErrorKind::BrokenPipe.into()),
                    0 => std::thread::sleep(Duration::from_millis(1)),
                    _ => match self.file.read(&mut bytes[offset..]) {
                        Ok(0) => return Err(std::io::ErrorKind::UnexpectedEof.into()),
                        Ok(n) => offset += n,
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => (),
                        Err(e) => return Err(e),
                    },
                }
            }
            Ok(())
        }
    }
    impl Read for Input {
        fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
            if bytes.is_empty() {
                return Ok(0);
            }
            // The stager requests another block only after write + readback.
            println!("NXRS_UPDATE_ACK offset={}", self.offset);
            std::io::stdout().flush()?;
            if self.offset == self.length {
                let mut end = [0; 4];
                self.exact(&mut end)?;
                return if &end == b"DONE" {
                    Ok(0)
                } else {
                    Err(std::io::ErrorKind::InvalidData.into())
                };
            }
            let count = bytes.len().min((self.length - self.offset) as usize);
            // The USB console has only 63 usable receive slots. Pace each
            // 32-byte fragment separately from completed flash-write ACKs.
            for part in bytes[..count].chunks_mut(32) {
                self.exact(part)?;
                self.offset += part.len() as u32;
                println!("NXRS_UPDATE_RX offset={}", self.offset);
                std::io::stdout().flush()?;
            }
            Ok(count)
        }
    }

    #[cfg(target_os = "nuttx")]
    pub fn run() -> Result<(), String> {
        use boot::{Control, State};
        use std::os::fd::AsFd;
        use wifi::QuiescenceControl;
        unsafe extern "C" {
            fn nxrs_detector_console_mode(saved: *mut u32, raw: i32) -> i32;
        }
        struct Raw([u32; 6]);
        impl Drop for Raw {
            fn drop(&mut self) {
                // SAFETY: restore the target-header fields saved by this owner.
                let _ = unsafe { nxrs_detector_console_mode(self.0.as_mut_ptr(), 0) };
            }
        }

        // The camera composition has returned only after capture, TLS, detector,
        // Wi-Fi and the retained listener have joined and released their owners.
        let mut quiet = wifi::open_for_quiescence().map_err(|e| format!("quiet {e:?}"))?;
        quiet.quiesce().map_err(|e| format!("quiesce {e:?}"))?;
        let descriptor = std::io::stdin()
            .as_fd()
            .try_clone_to_owned()
            .map_err(|e| format!("stdin {e:?}"))?;
        let mut saved = [0; 6];
        // SAFETY: the existing fixture shim saves six scalar termios fields.
        if unsafe { nxrs_detector_console_mode(saved.as_mut_ptr(), 1) } != 0 {
            return Err("raw input".into());
        }
        let _raw = Raw(saved);
        let mut source = Input {
            file: descriptor.into(),
            deadline: Instant::now() + Duration::from_secs(300),
            length: 0,
            offset: 0,
        };
        println!("NXRS_UPDATE_READY");
        std::io::stdout().flush().map_err(|_| "ready output")?;
        let mut header = [0; 8];
        source
            .exact(&mut header)
            .map_err(|e| format!("header {e:?}"))?;
        source.length = u32::from_be_bytes(header[4..].try_into().unwrap());
        if &header[..4] != b"NXUP" || !(32..=2 * 1024 * 1024 - 8192).contains(&source.length) {
            return Err("invalid frame".into());
        }
        let mut control = boot::open().map_err(|e| format!("boot {e:?}"))?;
        let length = source.length;
        control
            .stage_update(&mut source, length)
            .map_err(|e| format!("stage {e:?}"))?;
        if control.state().map_err(|e| format!("state {e:?}"))? != State::Stable {
            return Err("staging changed active state".into());
        }
        control
            .request_trial()
            .map_err(|e| format!("trial {e:?}"))?;
        if control.state().map_err(|e| format!("pending {e:?}"))? != State::Pending {
            return Err("missing pending state".into());
        }
        quiet.resume().map_err(|e| format!("resume {e:?}"))?;
        println!(
            "NXRS_UPDATE_PASS bytes={} stable=1 pending=1 resumed=1",
            source.length
        );
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[unsafe(no_mangle)]
        extern "C" fn nxrs_stream_console_ready() -> i32 {
            1
        }

        fn source(bytes: &[u8], length: u32) -> Input {
            use std::io::{Seek, SeekFrom};
            let path = std::env::temp_dir().join(format!(
                "nxrs-update-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            let mut file = std::fs::OpenOptions::new()
                .create_new(true)
                .read(true)
                .write(true)
                .open(&path)
                .unwrap();
            std::fs::remove_file(path).unwrap();
            file.write_all(bytes).unwrap();
            file.seek(SeekFrom::Start(0)).unwrap();
            Input {
                file,
                length,
                offset: 0,
                deadline: Instant::now() + Duration::from_secs(1),
            }
        }
        #[test]
        fn framed_payload_and_terminator_are_required() {
            for length in [32, 255, 256, 257] {
                let mut frame = vec![0xa5; length];
                frame.extend_from_slice(b"DONE");
                let mut input = source(&frame, length as u32);
                let mut body = [0; 256];
                let mut received = 0;
                loop {
                    let count = input.read(&mut body).unwrap();
                    if count == 0 {
                        break;
                    }
                    assert!(body[..count].iter().all(|&b| b == 0xa5));
                    received += count;
                }
                assert_eq!(received, length);
            }
            for frame in [&b"short"[..], &[0xa5; 32][..], &[0xa5; 36][..]] {
                assert!(source(frame, 32).read_to_end(&mut Vec::new()).is_err());
            }
        }
        #[test]
        fn expired_source_fails_before_reading() {
            let mut input = source(&[0xa5; 32], 32);
            input.deadline = Instant::now();
            assert_eq!(
                input.read(&mut [0; 32]).unwrap_err().kind(),
                std::io::ErrorKind::TimedOut
            );
            assert_eq!(input.offset, 0);
        }
    }
}

fn main() {
    #[cfg(target_os = "nuttx")]
    if let Err(error) = camera::device::run().and_then(|()| update::run()) {
        eprintln!("NXRS_UPDATE_FAIL {error}");
        std::process::exit(1);
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        eprintln!("requires the qualified NuttX camera fixture");
        std::process::exit(2);
    }
}
