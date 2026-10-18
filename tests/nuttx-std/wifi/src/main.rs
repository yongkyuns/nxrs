//! Real scan, single-wait owner on main, one native provider worker.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(target_os = "nuttx")]
use wifi_scan_owner::station;

use std::time::Instant;
use wifi::{
    Command, Completion, ControllerError, DeliveryError, Error, EventSink, ProviderStatus,
    Rejected, Update,
};
use wifi_scan_owner::{channels, CommandSender, OwnerReport, StopAdmissionError, StopHandle};

struct Observer {
    commands: CommandSender,
    stop: StopHandle,
    submitted: bool,
    observed: u32,
    outcome: Option<Result<Completion, Error>>,
}

impl Observer {
    fn submit(&mut self) -> Result<(), DeliveryError> {
        if !self.submitted {
            self.commands
                .try_submit(Command::Scan, 10_000)
                .map_err(|rejected| rejected.error)?;
            self.submitted = true;
        }
        Ok(())
    }
    fn stop(&self) -> Result<(), DeliveryError> {
        match self.stop.request_stop() {
            Ok(()) | Err(StopAdmissionError::AlreadyPending) => Ok(()),
            Err(StopAdmissionError::OwnerClosed) => Err(DeliveryError::Closed),
        }
    }
    fn observe(&mut self, report: &OwnerReport) -> Result<(), DeliveryError> {
        match report {
            OwnerReport::State {
                status: ProviderStatus::Ready,
                ..
            } => self.submit()?,
            OwnerReport::Update(Update::ScanResult(_)) => self.observed += 1,
            OwnerReport::Update(Update::Finished(outcome)) => {
                self.outcome = Some(outcome.result);
                self.stop()?;
            }
            OwnerReport::CommandRejected(rejected) => {
                self.outcome = Some(Err(match rejected.error {
                    ControllerError::Device(error) => error,
                    _ => Error::Protocol,
                }));
                self.stop()?;
            }
            _ => {}
        }
        Ok(())
    }
}

impl EventSink<OwnerReport> for Observer {
    fn try_emit(
        &mut self,
        report: OwnerReport,
    ) -> Result<(), Rejected<OwnerReport, DeliveryError>> {
        // Bounded bookkeeping and nonblocking queue admission only. No console
        // I/O, forwarding queue, native wait, handler recursion or worker join.
        self.observe(&report).map_err(|error| Rejected {
            value: report,
            error,
        })
    }
}

struct DrainReports;

impl EventSink<OwnerReport> for DrainReports {
    fn try_emit(&mut self, _: OwnerReport) -> Result<(), Rejected<OwnerReport, DeliveryError>> {
        Ok(())
    }
}

fn main() {
    #[cfg(target_os = "nuttx")]
    if std::env::args().nth(1).as_deref() == Some("station") {
        let peer = std::env::args()
            .nth(2)
            .expect("station requires MAC_IPV4:PORT")
            .parse()
            .expect("IPv4 peer");
        if let Err(error) = station::run(peer) {
            eprintln!("NXRS_WIFI_STATION_FAIL error={error:?}");
            std::process::exit(1);
        }
        return;
    }
    let fail = match std::env::args().nth(1).as_deref() {
        None => false,
        Some("fail") => true,
        _ => panic!("usage: wifi_test [fail | station MAC_IPV4:PORT]"),
    };
    let (mut owner, stop, commands, events) = channels(4, 4).expect("owner channels");
    let mut controller = wifi::open(events).unwrap_or_else(|error| {
        eprintln!("NXRS_WIFI_SCAN_FAIL acquisition={error:?}");
        std::process::exit(1);
    });
    let mut observer = Observer {
        commands,
        stop,
        submitted: false,
        observed: 0,
        outcome: None,
    };
    // The provider may have become Ready before composition captured it.
    if controller.status() == ProviderStatus::Ready {
        observer.submit().expect("scan admission");
    }
    let start = Instant::now();
    let mut clock = || start.elapsed().as_millis().try_into().unwrap();
    let result = owner.run(&mut controller, &mut observer, &mut clock);
    if result.is_err() {
        // A rejected report is not quiescence. Close native admission and keep
        // driving shutdown before joining; preserve the original run error.
        controller.request_stop().expect("recovery stop");
        owner
            .run(&mut controller, &mut DrainReports, &mut clock)
            .expect("recovery quiescence");
    }
    let reclaimed = controller.finish();
    reclaimed.expect("provider join");
    if let Err(error) = result {
        eprintln!("NXRS_WIFI_SCAN_FAIL owner={error:?}");
        std::process::exit(1);
    }
    let completion = observer
        .outcome
        .expect("scan outcome")
        .unwrap_or_else(|error| {
            eprintln!("NXRS_WIFI_SCAN_FAIL scan={error:?}");
            std::process::exit(1);
        });
    let reported = match completion {
        Completion::Scan { reported, .. } => reported,
        _ => panic!("wrong completion kind"),
    };
    assert_eq!(
        reported, observer.observed,
        "completion overtook scan results"
    );
    if fail {
        println!("NXRS_WIFI_SCAN_INJECTED_FAILURE");
        std::process::exit(7);
    }
    // Diagnostic formatting is outside the event loop, not a production logger.
    println!("NXRS_WIFI_SCAN_PASS results={reported} shutdown=joined");
}
