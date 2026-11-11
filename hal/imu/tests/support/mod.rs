//! Native-operation fakes and real sensor/resource lifecycle witnesses.
use super::device_state::{self, State};
use core::sync::atomic::{AtomicBool, Ordering};
use std::{
    format,
    fs::{self, File, OpenOptions},
    io,
    os::{fd::OwnedFd, unix::net::UnixStream},
    path::PathBuf,
    sync::Mutex,
    vec::Vec,
};

static DEVICE_STATE: device_state::DeviceState = device_state::DeviceState::new();
static SERIAL: Mutex<()> = Mutex::new(());

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Op {
    Stop,
    Detach,
}

struct Fake {
    regs: [u8; 128],
    fifo: Vec<u8>,
    bad_init: bool,
    bad_config_readback: bool,
    fail_register: bool,
    fail_unregister: bool,
    fail_stop: bool,
    fail_reference: bool,
    irq_asserted: bool,
    periodic_wake: bool,
    repeat_periodic: bool,
    registrations: usize,
    unregisters: usize,
    transfers: usize,
    last_endpoint: Option<(u8, u32)>,
    ops: Vec<Op>,
}

impl Fake {
    const fn new() -> Self {
        let mut regs = [0; 128];
        regs[0] = 0x24;
        regs[0x03] = 0xc0;
        regs[0x21] = 1;
        regs[0x0f] = 0x6c;
        Self {
            regs,
            fifo: Vec::new(),
            bad_init: false,
            bad_config_readback: false,
            fail_register: false,
            fail_unregister: false,
            fail_stop: false,
            fail_reference: false,
            irq_asserted: true,
            periodic_wake: false,
            repeat_periodic: false,
            registrations: 0,
            unregisters: 0,
            transfers: 0,
            last_endpoint: None,
            ops: Vec::new(),
        }
    }
}

static FAKE: Mutex<Fake> = Mutex::new(Fake::new());

pub(super) mod ffi {
    use super::*;
    thread_local! { pub static FAIL_PIPE: std::cell::Cell<bool> = const { std::cell::Cell::new(false) }; }
    pub(crate) fn pipe() -> io::Result<(File, File)> {
        if FAIL_PIPE.with(|fail| fail.replace(false)) {
            return Err(io::Error::other("injected cancellation setup failure"));
        }
        let (read, write) = UnixStream::pair()?;
        Ok((OwnedFd::from(read).into(), OwnedFd::from(write).into()))
    }

    pub(crate) fn transfer(
        _: i32,
        address: u8,
        frequency: u32,
        w: &[u8],
        r: &mut [u8],
    ) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.transfers += 1;
        f.last_endpoint = Some((address, frequency));
        let reg = *w
            .first()
            .ok_or_else(|| io::Error::other("missing register"))? as usize;
        if r.is_empty() {
            if f.fail_reference && w == [0x54, 0x0a] {
                f.regs[reg] = w[1];
                return Err(io::Error::other("reference output write acknowledgement"));
            }
            if reg == 0x58 && w.get(1) == Some(&0) {
                f.ops.push(Op::Stop);
                if f.fail_stop {
                    return Err(io::Error::other("sensor stop"));
                }
            }
            if address == 0x6a && w == [0x12, 1] {
                f.regs[reg] = 0;
            } else if reg != 0x5e {
                let end = reg + w.len() - 1;
                f.regs[reg..end].copy_from_slice(&w[1..]);
            }
        } else {
            match reg {
                0x03 => r.fill(0xc0),
                0x21 => r.fill(if f.bad_init { 2 } else { 1 }),
                0x1d => r.fill(f.regs[0x1d]),
                0x24 => r.copy_from_slice(&(f.fifo.len() as u16).to_le_bytes()),
                0x25 => r.fill(0),
                0x3a if address == 0x6a => {
                    r.copy_from_slice(&((f.fifo.len() / 7) as u16).to_le_bytes())
                }
                0x26 | 0x78 => {
                    for byte in r {
                        *byte = f.fifo.remove(0);
                    }
                }
                0x41 if f.bad_config_readback => r.fill(f.regs[reg] ^ 1),
                _ => r.copy_from_slice(&f.regs[reg..reg + r.len()]),
            }
        }
        Ok(())
    }

    pub(crate) fn wait(_: i32, _: i32) -> io::Result<bool> {
        Ok(false)
    }
    pub(crate) fn tick(_: i32, timeout: i32) -> io::Result<bool> {
        let wake = {
            let mut fake = FAKE.lock().unwrap();
            fake.repeat_periodic || std::mem::take(&mut fake.periodic_wake)
        };
        if wake {
            std::thread::sleep(std::time::Duration::from_millis(timeout as u64));
        }
        Ok(wake)
    }
    pub(crate) fn irq_check(_: i32) -> io::Result<()> {
        Ok(())
    }
    pub(crate) fn irq_read(_: i32) -> io::Result<bool> {
        Ok(FAKE.lock().unwrap().irq_asserted)
    }
    pub(crate) fn irq_register(_: i32) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.registrations += 1;
        if f.fail_register {
            Err(io::Error::other("IRQ register"))
        } else {
            Ok(())
        }
    }
    pub(crate) fn irq_unregister(_: i32) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.registrations -= 1;
        f.unregisters += 1;
        f.ops.push(Op::Detach);
        if f.fail_unregister {
            Err(io::Error::other("IRQ unregister"))
        } else {
            Ok(())
        }
    }
}

// Decode chip-specific SPI wire bytes independently before reusing the register model.
// This exercises the real SPI resource and observed-bus wrappers, not a host provider.
pub(super) mod spi_ffi {
    use super::*;
    pub(crate) fn transfer(
        fd: i32,
        select: u16,
        frequency: u32,
        mode: u8,
        tx: &[u8],
        rx: &mut [u8],
    ) -> io::Result<()> {
        assert!(select <= 1);
        assert_eq!((frequency, mode), (4_000_000, 3));
        let address = if select == 0 { 0x68 } else { 0x6a };
        let register = tx[0] & 0x7f;
        if tx[0] & 0x80 != 0 {
            let prefix = if select == 0 { 2 } else { 1 };
            assert!(tx[1..].iter().all(|byte| *byte == 0));
            ffi::transfer(fd, address, frequency, &[register], &mut rx[prefix..])
        } else {
            ffi::transfer(fd, address, frequency, tx, &mut [])
        }
    }
}

use super::{device, signal};
use acquisition::probe as trace;
use acquisition::readiness;
#[allow(dead_code, reason = "The fixture uses contextual GPIO cleanup.")]
#[path = "../../../../driver/gpio/tests/support/mod.rs"]
pub(super) mod gpio_resource;
#[path = "../../../../driver/i2c/tests/support/mod.rs"]
mod resource;
#[allow(dead_code)]
#[path = "../../../../platform/src/nuttx/imu_resources.rs"]
mod resources;
#[path = "../../../../driver/spi/tests/support/mod.rs"]
mod spi_resource;
use crate::DeviceError;
use acquisition::probe::{Observed, Trace};
use peripheral::imu::Imu as ImuDevice;

type Bmi270Sensor = nxrs_bmi270::Bmi270<resources::Bus, resources::Delay>;
type Lsm6dsoSensor = nxrs_lsm6dso::Lsm6dso<resources::Bus>;
type I2cBinding<'a> = ((resource::Config<'a>, u8), Option<&'a str>);

fn acquire_bmi270(
    binding: I2cBinding<'_>,
    config: crate::Config,
) -> Result<device::Device<Bmi270Sensor>, crate::DeviceError> {
    acquire_bmi270_traced(binding, config, trace::Trace::default())
}

fn acquire_bmi270_traced(
    (binding, irq_path): I2cBinding<'_>,
    config: crate::Config,
    trace: trace::Trace,
) -> Result<device::Device<Bmi270Sensor>, crate::DeviceError> {
    let observed = trace.clone();
    device::Device::open(
        config,
        irq_path,
        trace,
        &DEVICE_STATE,
        || resources::open_i2c(binding, observed),
        |(bus, address), settings| {
            Bmi270Sensor::open_configured(bus, address, resources::Delay, settings)
                .map_err(Into::into)
        },
    )
}

fn acquire_lsm6dso(
    (binding, irq_path): I2cBinding<'_>,
    config: crate::Config,
) -> Result<device::Device<Lsm6dsoSensor>, crate::DeviceError> {
    device::Device::open(
        config,
        irq_path,
        trace::Trace::default(),
        &DEVICE_STATE,
        || resources::open_i2c(binding, trace::Trace::default()),
        |(bus, address), settings| {
            Lsm6dsoSensor::open_configured(bus, address, resources::Delay, settings)
                .map_err(Into::into)
        },
    )
}

struct DeviceFile(PathBuf);
impl DeviceFile {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("nxrs-imu-resource-test-{}", std::process::id()));
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        Self(path)
    }
    fn path(&self) -> &str {
        self.0.to_str().unwrap()
    }
    fn missing_path(&self) -> PathBuf {
        self.0.with_extension("missing")
    }
}
impl Drop for DeviceFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn run<S: ImuDevice>(
    mut device: device::Device<S>,
    stop: &AtomicBool,
    wake: &File,
    emit: &mut dyn FnMut(crate::Event) -> bool,
) -> Result<(), crate::DeviceError> {
    device.run(stop, wake, emit)
}

fn assert_state(expected: State) {
    assert_eq!(DEVICE_STATE.value.load(Ordering::Relaxed), expected as u8);
}

fn reset() {
    DEVICE_STATE
        .value
        .store(State::Available as u8, Ordering::Relaxed);
    *FAKE.lock().unwrap() = Fake::new();
}
fn binding<'a>(path: &'a str, irq_path: Option<&'a str>) -> I2cBinding<'a> {
    (
        (
            resource::Config {
                path,
                frequency: 400_000,
            },
            0x68,
        ),
        irq_path,
    )
}

fn default_config() -> crate::Config {
    Bmi270Sensor::DEFAULT
}

#[test]
fn irq_registration_rollback_and_fault_marking_are_not_repeated() {
    let _serial = SERIAL.lock().unwrap();
    for (fail_register, fail_unregister) in
        [(false, false), (true, false), (true, true), (false, true)]
    {
        reset();
        let file = DeviceFile::new();
        FAKE.lock().unwrap().fail_register = fail_register;
        FAKE.lock().unwrap().fail_unregister = fail_unregister;
        match device::open_irq(file.path(), &DEVICE_STATE) {
            Err(crate::DeviceError::Io) => assert!(fail_register),
            Ok(mut irq) => {
                assert!(!fail_register);
                assert!(irq.read().unwrap());
                assert_eq!(irq.unregister().is_err(), fail_unregister);
                assert!(irq.unregister().is_ok());
            }
            _ => panic!("unexpected registration result"),
        }
        let f = FAKE.lock().unwrap();
        assert_eq!(f.registrations, 0);
        assert_eq!(f.unregisters, 1);
        assert_state(if fail_unregister {
            State::Faulted
        } else {
            State::Available
        });
    }
}

#[test]
fn configured_irq_failure_has_no_timer_fallback_and_i2c_failure_detaches_irq() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    let missing = file.missing_path();
    let missing = missing.to_str().unwrap();

    reset();
    assert_eq!(
        acquire_bmi270(binding(file.path(), Some(missing)), default_config()).err(),
        Some(crate::DeviceError::Unsupported)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, 0);
    assert_state(State::Available);

    for fail_unregister in [false, true] {
        reset();
        FAKE.lock().unwrap().fail_unregister = fail_unregister;
        assert_eq!(
            acquire_bmi270(binding(missing, Some(file.path())), default_config()).err(),
            Some(crate::DeviceError::Io)
        );
        let f = FAKE.lock().unwrap();
        assert_eq!((f.registrations, f.unregisters), (0, 1));
        assert_state(if fail_unregister {
            State::Faulted
        } else {
            State::Available
        });
    }
}

#[test]
fn rejected_requests_do_not_acquire_resources() {
    use crate::DeviceError::{InvalidData, Unsupported};
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    for (min, max, accel, gyro, interval, error) in [
        (25_000, 104_000, 4, 2000, 5_000, Unsupported),
        (25_000, 104_000, 3, 2000, 40_000, Unsupported),
        (25_000, 104_000, 4, 333, 40_000, Unsupported),
        (0, 104_000, 4, 2000, 40_000, InvalidData),
        (104_000, 25_000, 4, 2000, 40_000, InvalidData),
        (25_000, 104_000, 0, 2000, 40_000, InvalidData),
        (25_000, 104_000, 4, 0, 40_000, InvalidData),
        (25_000, 104_000, 4, 2000, 0, InvalidData),
    ] {
        let config = crate::Config {
            min_rate_millihz: min,
            max_rate_millihz: max,
            accel_range_g: accel,
            gyro_range_dps: gyro,
            max_delivery_interval_us: interval,
        };
        assert_eq!(
            acquire_bmi270(binding(file.path(), Some(file.path())), config).err(),
            Some(error)
        );
        assert_eq!(
            acquire_lsm6dso(binding(file.path(), Some(file.path())), config).err(),
            Some(error)
        );
    }
    let f = FAKE.lock().unwrap();
    assert_eq!(f.transfers, 0);
    assert_eq!((f.registrations, f.unregisters), (0, 0));
    assert_state(State::Available);
}

#[test]
fn both_declared_bmi270_endpoints_reach_the_same_bus_adapter() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    let alternate = file.missing_path();
    let _file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&alternate)
        .unwrap();
    let alternate = DeviceFile(alternate);
    for (path, address, frequency) in [
        (file.path(), 0x68, 400_000),
        (alternate.path(), 0x69, 100_000),
    ] {
        reset();
        let binding = ((resource::Config { path, frequency }, address), None);
        let device = acquire_bmi270(binding, default_config()).unwrap();
        assert_eq!(
            FAKE.lock().unwrap().last_endpoint,
            Some((address, frequency))
        );
        drop(device);
        assert_state(State::Available);
    }
}

#[test]
fn replacement_sensor_uses_the_same_state_and_reports_its_exact_rate() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    let replacement = || {
        (
            (
                resource::Config {
                    path: file.path(),
                    frequency: 100_000,
                },
                0x6a,
            ),
            None,
        )
    };
    let default = Lsm6dsoSensor::DEFAULT;
    assert_eq!(
        acquire_lsm6dso(replacement(), default_config()).err(),
        Some(crate::DeviceError::Unsupported)
    );
    let device = acquire_lsm6dso(replacement(), default).unwrap();
    assert_eq!(device.applied_config().rate_millihz, 104_000);
    assert_eq!(device.applied_config().delivery_interval_us, 9_616);
    assert_eq!(FAKE.lock().unwrap().last_endpoint, Some((0x6a, 100_000)));
    // The accepted 104 Hz / 4 g / 2000 dps reaches the sensor's control registers.
    assert_eq!(&FAKE.lock().unwrap().regs[0x10..=0x11], &[0x48, 0x4c]);
    assert_eq!(
        acquire_bmi270(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::Busy)
    );
    drop(device);
    assert_state(State::Available);
}

#[test]
fn irq_cleanup_faults_only_its_instance() {
    static LEFT: device_state::DeviceState = device_state::DeviceState::new();
    static RIGHT: device_state::DeviceState = device_state::DeviceState::new();
    let _serial = SERIAL.lock().unwrap();
    reset();
    let file = DeviceFile::new();
    let mut irq = device::open_irq(file.path(), &LEFT).unwrap();
    FAKE.lock().unwrap().fail_unregister = true;
    assert!(irq.unregister().is_err());
    assert!(matches!(LEFT.begin_use(), Err(crate::DeviceError::Io)));
    assert!(RIGHT.begin_use().is_ok());
    drop(irq);
    assert_eq!(FAKE.lock().unwrap().unregisters, 1);
}

#[test]
fn applied_config_matches_default_and_reports_a_negotiated_nondefault_rate() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    let defaults = default_config();
    let expected_defaults = crate::AppliedConfig {
        rate_millihz: 100_000,
        accel_range_g: 4,
        gyro_range_dps: 2000,
        delivery_interval_us: 10_000,
    };
    let device = acquire_bmi270(binding(file.path(), None), defaults).unwrap();
    assert_eq!(device.applied_config(), expected_defaults);
    drop(device);

    reset();
    let requested = crate::Config {
        min_rate_millihz: 25_000,
        max_rate_millihz: 100_000,
        accel_range_g: 8,
        gyro_range_dps: 125,
        max_delivery_interval_us: 20_000,
    };
    let device = acquire_bmi270(binding(file.path(), None), requested).unwrap();
    let applied = device.applied_config();
    assert_eq!(applied.rate_millihz, 50_000);
    assert_eq!(applied.accel_range_g, 8);
    assert_eq!(applied.gyro_range_dps, 125);
    assert_eq!(applied.delivery_interval_us, 20_000);
    assert_ne!(applied, expected_defaults);
    // Both ODR codes are 50 Hz, acceleration is 8 g, and gyro is 125 dps
    // with the driver's existing 2000 dps OIS setting in bit 3.
    let regs = FAKE.lock().unwrap().regs;
    assert_eq!((regs[0x40] & 0x0f, regs[0x42] & 0x0f), (7, 7));
    assert_eq!((regs[0x41] & 0x03, regs[0x43] & 0x0f), (2, 0x0c));
    drop(device);
}

#[test]
fn partial_sensor_setup_marks_faulted() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    FAKE.lock().unwrap().bad_init = true;
    assert_eq!(
        acquire_bmi270(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::InvalidData)
    );
    assert_state(State::Faulted);
    let transfers = FAKE.lock().unwrap().transfers;
    assert_eq!(
        acquire_bmi270(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::Io)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, transfers);
}

#[test]
fn config_readback_mismatch_faults_before_publishing_device() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    FAKE.lock().unwrap().bad_config_readback = true;
    assert_eq!(
        acquire_bmi270(binding(file.path(), Some(file.path())), default_config()).err(),
        Some(crate::DeviceError::InvalidData)
    );
    assert_state(State::Faulted);
    let transfers = {
        let fake = FAKE.lock().unwrap();
        assert_eq!((fake.registrations, fake.unregisters), (0, 1));
        fake.transfers
    };
    assert_eq!(
        acquire_bmi270(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::Io)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, transfers);
}

fn join_device(device: device::Device<Bmi270Sensor>) {
    let cancel = readiness::Cancel::new(|| {
        let (reader, writer) = UnixStream::pair()?;
        Ok((OwnedFd::from(reader).into(), OwnedFd::from(writer).into()))
    })
    .unwrap();
    let wake = cancel.reader.clone();
    let mut worker = acquisition::Worker::start(
        "imu-test",
        16384,
        cancel,
        |_| Ok(()),
        crate::Event::Failed,
        move |stop, emit| run(device, stop, &wake, emit),
    )
    .unwrap();
    crate::Session::join(&mut worker).unwrap();
    assert!(worker.is_finished());
    assert_eq!(FAKE.lock().unwrap().ops, [Op::Stop, Op::Detach]);
    assert_state(State::Available);
}

#[test]
fn unpublished_device_and_joined_worker_both_stop_before_detach() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    // A cancel setup or thread spawn failure drops the unpublished state.
    drop(acquire_bmi270(binding(file.path(), Some(file.path())), default_config()).unwrap());
    assert_eq!(FAKE.lock().unwrap().ops, [Op::Stop, Op::Detach]);
    assert_state(State::Available);

    reset();
    FAKE.lock().unwrap().irq_asserted = false;
    let device = acquire_bmi270(binding(file.path(), Some(file.path())), default_config()).unwrap();
    join_device(device);
}

#[test]
fn hal_adds_stream_metadata_and_preserves_device_overflow() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    for overflow in [false, true] {
        reset();
        let device =
            acquire_bmi270(binding(file.path(), Some(file.path())), default_config()).unwrap();
        {
            let mut fake = FAKE.lock().unwrap();
            fake.regs[0x1d] = u8::from(overflow);
            for _ in 0..3 {
                for axis in [0_i16, 0, 16384, 8192, -8192, 0] {
                    fake.fifo.extend_from_slice(&axis.to_le_bytes());
                }
            }
        }
        let mut samples = Vec::new();
        let result = run(
            device,
            &AtomicBool::new(false),
            &File::open(file.path()).unwrap(),
            &mut |event| {
                let crate::Event::Sample(sample) = event else {
                    panic!("unexpected event")
                };
                samples.push(sample);
                samples.len() < 3
            },
        );
        if overflow {
            assert_eq!(result, Err(crate::DeviceError::Full));
            assert!(samples.is_empty());
        } else {
            result.unwrap();
            assert_eq!(samples.len(), 3);
            for (i, sample) in samples.iter().enumerate() {
                assert_eq!(sample.sequence, i as u64 + 1);
                assert_eq!(sample.timestamp_ms, samples[0].timestamp_ms);
                assert_eq!(sample.sample_interval_us, 10_000);
                assert!((sample.accel_mps2[0] - 9.80665).abs() < 1e-5);
                assert!((sample.gyro_rps[2] - 17.453293).abs() < 1e-5);
            }
        }
        assert_eq!(FAKE.lock().unwrap().ops, [Op::Stop, Op::Detach]);
        assert_state(State::Available);
    }
}

#[test]
fn failed_stop_or_detach_marks_faulted_without_repeating_release() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    for (fail_stop, fail_unregister) in [(true, false), (false, true)] {
        reset();
        {
            let mut f = FAKE.lock().unwrap();
            f.fail_stop = fail_stop;
            f.fail_unregister = fail_unregister;
        }
        let device =
            acquire_bmi270(binding(file.path(), Some(file.path())), default_config()).unwrap();
        assert_eq!(
            run(
                device,
                &AtomicBool::new(true),
                &File::open(file.path()).unwrap(),
                &mut |_| true
            ),
            Err(crate::DeviceError::Io)
        );
        let f = FAKE.lock().unwrap();
        assert_state(State::Faulted);
        assert_eq!((f.registrations, f.unregisters), (0, 1));
        assert_eq!(f.ops, [Op::Stop, Op::Detach]);
    }
}

#[test]
fn partial_reference_setup_releases_the_sensor_and_checks_irq_detach() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    for fail_unregister in [false, true] {
        reset();
        {
            let mut fake = FAKE.lock().unwrap();
            fake.fail_reference = true;
            fake.fail_unregister = fail_unregister;
        }
        assert_eq!(
            acquire_bmi270(binding(file.path(), Some(file.path())), default_config())
                .and_then(|device| device
                    .prepare(|sensor| sensor.enable_sensor_reference().map_err(Into::into)))
                .err(),
            Some(crate::DeviceError::Io)
        );
        let fake = FAKE.lock().unwrap();
        assert_eq!(fake.ops, [Op::Stop, Op::Detach]);
        assert_eq!(fake.regs[0x54] & 0x08, 0);
        assert_eq!(fake.regs[0x58], 0);
        assert_state(if fail_unregister {
            State::Faulted
        } else {
            State::Available
        });
    }
}

#[cfg(feature = "probe")]
mod diagnostics {
    use super::*;

    #[test]
    fn timing_marks_worker_io_between_setup_and_cleanup() {
        use acquisition::probe::Timing;
        use std::sync::Arc;

        let _serial = SERIAL.lock().unwrap();
        let file = DeviceFile::new();
        reset();
        let events = Arc::new(Mutex::new(Vec::new()));
        let captured = events.clone();
        let trace = trace::Trace::new(move |event| captured.lock().unwrap().push(event)).unwrap();
        let device = acquire_bmi270_traced(
            binding(file.path(), Some(file.path())),
            default_config(),
            trace,
        )
        .unwrap();
        assert_eq!(
            run(
                device,
                &AtomicBool::new(false),
                &File::open(file.path()).unwrap(),
                &mut |_| true
            ),
            Err(crate::DeviceError::Io), // The fake IRQ stays asserted after an empty drain.
        );
        assert_eq!(
            Arc::strong_count(&events),
            1,
            "observer released after cleanup"
        );
        let events = events.lock().unwrap();
        let wake = events
            .iter()
            .position(|event| {
                matches!(
                    event,
                    Timing::Wake {
                        periodic_deadline: None,
                        ..
                    }
                )
            })
            .unwrap();
        let drain = events
            .iter()
            .position(|event| matches!(event, Timing::Drain { complete: true, .. }))
            .unwrap();
        assert!(wake > 0 && drain > wake + 1 && drain + 1 < events.len());
        assert!(events
            .iter()
            .enumerate()
            .all(|(i, event)| i == wake || i == drain || matches!(event, Timing::Ioctl { .. })));
    }

    #[test]
    fn joined_worker_releases_its_observer_after_cleanup() {
        use std::sync::Arc;
        let _serial = SERIAL.lock().unwrap();
        let file = DeviceFile::new();
        reset();
        FAKE.lock().unwrap().irq_asserted = false;
        let retained = Arc::new(());
        let observer = retained.clone();
        let trace = trace::Trace::new(move |_| {
            let _ = &observer;
        })
        .unwrap();
        let device = acquire_bmi270_traced(
            binding(file.path(), Some(file.path())),
            default_config(),
            trace,
        )
        .unwrap();
        assert_eq!(Arc::strong_count(&retained), 2);
        join_device(device);
        assert_eq!(
            Arc::strong_count(&retained),
            1,
            "join released the observer"
        );
    }
}

#[test]
fn spi_factories_share_device_state_and_cleanup() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    fn verify<S: ImuDevice>(device: device::Device<S>) {
        assert_state(State::InUse);
        FAKE.lock().unwrap().periodic_wake = true;
        let mut samples = Vec::new();
        let expected_interval = 1_000_000_000u32.div_ceil(device.applied_config().rate_millihz);
        run(
            device,
            &AtomicBool::new(false),
            &File::open("/dev/null").unwrap(),
            &mut |event| {
                if let crate::Event::Sample(sample) = event {
                    samples.push(sample);
                }
                true
            },
        )
        .unwrap();
        assert_eq!(samples.len(), 1);
        assert_eq!(samples[0].sequence, 1);
        assert_eq!(samples[0].sample_interval_us, expected_interval);
        assert!((samples[0].accel_mps2[0] - 9.80665).abs() < 0.01);
        assert_state(State::Available);
    }
    for select in [0, 1] {
        reset();
        let binding = spi_resource::Config {
            path: file.path(),
            chip_select: select,
            frequency: 4_000_000,
            mode: 3,
        };
        {
            let mut fake = FAKE.lock().unwrap();
            for (sensor, xyz) in [(1u8, [0i16, 0, 16384]), (2, [8192, 0, 0])] {
                if select == 1 {
                    fake.fifo.push((sensor << 3) | 1);
                } // Even-parity LSM tags.
                for axis in xyz {
                    fake.fifo.extend_from_slice(&axis.to_le_bytes());
                }
            }
        }
        if select == 0 {
            type Sensor = nxrs_bmi270::Bmi270Spi<resources::Spi, resources::Delay>;
            verify(
                device::Device::open(
                    Sensor::DEFAULT,
                    None,
                    trace::Trace::default(),
                    &DEVICE_STATE,
                    || resources::open_spi(binding, trace::Trace::default()),
                    |bus, settings| {
                        Sensor::open_spi(bus, resources::Delay, settings).map_err(Into::into)
                    },
                )
                .unwrap(),
            );
            assert_eq!(FAKE.lock().unwrap().regs[0x7d], 0);
        } else {
            type Sensor = nxrs_lsm6dso::Lsm6dsoSpi<resources::Spi>;
            verify(
                device::Device::open(
                    Sensor::DEFAULT,
                    None,
                    trace::Trace::default(),
                    &DEVICE_STATE,
                    || resources::open_spi(binding, trace::Trace::default()),
                    |bus, settings| {
                        Sensor::open_spi(bus, resources::Delay, settings).map_err(Into::into)
                    },
                )
                .unwrap(),
            );
            assert_eq!(&FAKE.lock().unwrap().regs[0x10..=0x11], &[0, 0]);
        }
    }
    reset();
    FAKE.lock().unwrap().bad_init = true;
    type Sensor = nxrs_bmi270::Bmi270Spi<resources::Spi, resources::Delay>;
    let binding = spi_resource::Config {
        path: file.path(),
        chip_select: 0,
        frequency: 4_000_000,
        mode: 3,
    };
    let result = device::Device::open(
        Sensor::DEFAULT,
        None,
        trace::Trace::default(),
        &DEVICE_STATE,
        || resources::open_spi(binding, trace::Trace::default()),
        |bus, settings| Sensor::open_spi(bus, resources::Delay, settings).map_err(Into::into),
    );
    assert_eq!(result.err(), Some(crate::DeviceError::InvalidData));
    assert_state(State::Faulted);
}

#[test]
fn periodic_wait_keeps_early_deadlines_and_skips_missed_periods() {
    use std::time::{Duration, Instant};
    let start = Instant::now();
    let period = Duration::from_millis(10);
    let mut next_deadline = start + period;
    assert_eq!(
        signal::advance_periodic_deadline(
            &mut next_deadline,
            period,
            start + Duration::from_millis(9)
        ),
        None
    );
    assert_eq!(next_deadline, start + period);
    assert_eq!(
        signal::advance_periodic_deadline(&mut next_deadline, period, start + period),
        Some(start + period)
    );
    assert_eq!(next_deadline, start + period * 2);
    assert_eq!(
        signal::advance_periodic_deadline(&mut next_deadline, period, start + period * 10),
        Some(start + period * 2)
    );
    assert_eq!(
        signal::advance_periodic_deadline(&mut next_deadline, period, start + period * 10),
        None
    );
    assert_eq!(next_deadline, start + period * 11);
}

/// Use the same native boundary for normalized-sensor instance tests. Serialize
/// with the register-model tests so their one-shot wake controls stay isolated.
pub(super) fn periodic_fixture() -> std::sync::MutexGuard<'static, ()> {
    let serial = SERIAL.lock().unwrap();
    reset();
    FAKE.lock().unwrap().repeat_periodic = true;
    serial
}
