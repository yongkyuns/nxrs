use nxrs_controller_demo::{Command, Controller, SubmitError, parse_command};
use std::io::{self, Read, Write};
use std::time::Duration;

const HELP: &str = "use on, off, set HZ DUTY, status, stats, wait MS, stop";

fn execute(controller: &Controller, text: &str) -> Result<bool, String> {
    let text = text.trim();
    if text == "stop" {
        return Ok(false);
    }
    if let Some(value) = text.strip_prefix("wait ") {
        let milliseconds: u64 = value
            .parse()
            .map_err(|_| "wait requires 0..60000 milliseconds")?;
        if milliseconds > 60_000 {
            return Err("wait requires 0..60000 milliseconds".into());
        }
        // The command client waits; acquisition and the application owner keep running.
        std::thread::sleep(Duration::from_millis(milliseconds));
        return Ok(true);
    }
    let command = parse_command(text).map_err(|_| format!("unknown command {text:?} ({HELP})"))?;
    let ticket = controller
        .try_command(command)
        .map_err(|error| match error {
            SubmitError::Full(_) => "command queue is full".to_owned(),
            SubmitError::Disconnected(_) => "controller worker is unavailable".to_owned(),
        })?;
    let status = ticket
        .wait()
        .map_err(|_| "controller disappeared before completion".to_owned())?
        .map_err(|error| format!("device command failed: {error:?}"))?;
    if command == Command::Stats {
        let interval = status
            .observations
            .last_interval
            .map(|value| value.as_micros().to_string())
            .unwrap_or_else(|| "none".into());
        let fault = status
            .source
            .fault
            .map(|error| format!("{error:?}"))
            .unwrap_or_else(|| "none".into());
        let watchdog_fault = status
            .watchdog_fault
            .map(|error| format!("{error:?}"))
            .unwrap_or_else(|| "none".into());
        println!(
            "stats: observed={} rejected={} processed={} gaps={} max_dispatch_us={} last_interval_us={} fault={} watchdog_fault={}",
            status.source.observed,
            status.source.rejected,
            status.observations.processed,
            status.observations.sequence_gaps,
            status.observations.max_dispatch.as_micros(),
            interval,
            fault,
            watchdog_fault
        );
    } else {
        println!("{command}: {}", status.state);
    }
    Ok(true)
}

/// Fixed-size console lines keep malformed input from growing application memory.
fn read_line(input: &mut impl Read) -> Result<Option<String>, String> {
    let mut bytes = [0u8; 128];
    let mut length = 0;
    let mut overflow = false;
    loop {
        let mut byte = [0u8];
        match input.read(&mut byte) {
            Ok(0) if length == 0 && !overflow => return Ok(None),
            Ok(0) => break,
            Ok(_) if byte[0] == b'\n' => break,
            Ok(_) if byte[0] == b'\r' => continue,
            Ok(_) => {
                if length < bytes.len() {
                    bytes[length] = byte[0];
                    length += 1;
                } else {
                    overflow = true;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(format!("console read failed: {error}")),
        }
    }
    if overflow {
        return Err("command exceeds 128 bytes".into());
    }
    String::from_utf8(bytes[..length].to_vec())
        .map(Some)
        .map_err(|_| "command must be UTF-8".into())
}

fn interactive(controller: &Controller) -> Result<(), String> {
    let mut input = io::stdin().lock();
    loop {
        print!("controller> ");
        io::stdout()
            .flush()
            .map_err(|error| format!("console write failed: {error}"))?;
        let Some(line) = read_line(&mut input)? else {
            return Ok(());
        };
        if line.trim().is_empty() {
            continue;
        }
        if !execute(controller, &line)? {
            return Ok(());
        }
    }
}

fn batch(controller: &Controller, commands: &[String]) -> Result<(), String> {
    let mut commands = commands.iter();
    while let Some(word) = commands.next() {
        let mut command = word.clone();
        let parameters = match word.as_str() {
            "set" => 2,
            "wait" => 1,
            _ => 0,
        };
        for _ in 0..parameters {
            command.push(' ');
            command.push_str(
                commands
                    .next()
                    .ok_or_else(|| format!("incomplete command ({HELP})"))?,
            );
        }
        if !execute(controller, &command)? {
            return if commands.next().is_none() {
                Ok(())
            } else {
                Err("stop must be the last command".into())
            };
        }
    }
    Ok(())
}

fn run() -> Result<(), String> {
    let mut arguments: Vec<_> = std::env::args().skip(1).collect();
    if arguments.iter().any(|arg| arg == "--help") {
        println!(
            "controller [--watchdog MS] [--interactive | on|off|set HZ DUTY|status|stats|wait MS|stop ...]"
        );
        println!("controller --reset-cause");
        return Ok(());
    }
    if arguments.as_slice() == ["--reset-cause"] {
        let cause =
            boot::reset_cause().map_err(|error| format!("cannot read reset cause: {error:?}"))?;
        println!("reset cause: {cause:?}");
        return Ok(());
    }
    let timeout = if arguments.first().is_some_and(|arg| arg == "--watchdog") {
        let milliseconds = arguments
            .get(1)
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| (1000..=60_000).contains(value))
            .ok_or("--watchdog requires 1000..60000 milliseconds")?;
        arguments.drain(..2);
        Some(Duration::from_millis(milliseconds))
    } else {
        None
    };
    let controller =
        Controller::start_with_watchdog(pulse::open, timeout).map_err(|error| error.to_string())?;
    println!("ready: off");
    let commands = match arguments.as_slice() {
        [] => batch(&controller, &["on".into(), "status".into(), "off".into()]),
        [mode] if mode == "--interactive" => interactive(&controller),
        _ => batch(&controller, &arguments),
    };
    // All command, parsing, and console failures reach the same joined cleanup.
    let stopped = controller
        .stop()
        .map_err(|_| "controller worker panicked during shutdown".to_owned())?;
    println!("stopped: {}", stopped.status.state);
    let pulse_shutdown = stopped
        .shutdown
        .map(|_| ())
        .map_err(|error| format!("cannot shut down pulse device: {error:?}"));
    let watchdog_shutdown = stopped
        .watchdog
        .map_err(|error| format!("watchdog supervision failed: {error:?}"));
    let shutdown = match (pulse_shutdown, watchdog_shutdown) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(pulse), Err(watchdog)) => Err(format!("{pulse}; {watchdog}")),
    };
    match (commands, shutdown) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(command), Err(shutdown)) => Err(format!("{command}; {shutdown}")),
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("controller: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::read_line;

    #[test]
    fn console_lines_are_bounded_and_crlf_or_eof_are_supported() {
        assert_eq!(
            read_line(&mut &b"set 25 50\r\n"[..]).unwrap().as_deref(),
            Some("set 25 50")
        );
        assert_eq!(
            read_line(&mut &b"status"[..]).unwrap().as_deref(),
            Some("status")
        );
        assert!(read_line(&mut &b""[..]).unwrap().is_none());
        let data = [b'x'; 129];
        assert!(read_line(&mut data.as_slice()).is_err());
        assert!(read_line(&mut &b"\xff\n"[..]).is_err());
    }
}
