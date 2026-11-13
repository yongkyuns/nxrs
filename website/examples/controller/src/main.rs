use led::Led;
use nxrs_controller_demo::{Controller, SubmitError, parse_command};

fn execute<L, I, S>(controller: &Controller<L>, input: I) -> Result<(), String>
where
    L: Led + Send + 'static,
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut input = input.into_iter().peekable();
    while let Some(text) = input.next() {
        let text = text.as_ref();
        if text.trim() == "stop" {
            return if input.peek().is_none() {
                Ok(())
            } else {
                Err("stop must be the last command".into())
            };
        }
        let command = parse_command(text)
            .map_err(|_| format!("unknown command {text:?} (use on, off, status, stop)"))?;
        let ticket = controller
            .try_command(command)
            .map_err(|error| match error {
                SubmitError::Full(_) => "command queue is full".to_owned(),
                SubmitError::Disconnected(_) => "controller worker is unavailable".to_owned(),
            })?;
        let state = ticket
            .wait()
            .map_err(|_| "controller disappeared before completion".to_owned())?
            .map_err(|error| format!("output command failed: {error:?}"))?;
        println!("{command}: {state}");
    }
    Ok(())
}

fn run() -> Result<(), String> {
    let output = led::open().map_err(|error| format!("cannot acquire LED output: {error:?}"))?;
    let controller = Controller::start(output, 0).map_err(|error| error.to_string())?;
    println!("ready: off");

    let mut input = std::env::args().skip(1).peekable();
    let commands = if input.peek().is_none() {
        execute(&controller, ["on", "status", "off"])
    } else {
        execute(&controller, input)
    };

    // Even rejected input or a failed output operation reaches explicit cleanup.
    let stopped = controller
        .stop()
        .map_err(|_| "controller worker panicked during shutdown".to_owned())?;
    println!("stopped: {}", stopped.state);
    let shutdown = stopped
        .shutdown
        .map_err(|error| format!("cannot switch output off: {error:?}"));
    drop(stopped.output);
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
