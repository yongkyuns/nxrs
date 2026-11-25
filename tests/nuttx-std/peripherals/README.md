# WROOM and Pico 2 W peripheral qualification

```sh
cargo nxrs build --manifest-path tests/nuttx-std/peripherals/Cargo.toml \
  --profile esp32s3-peripherals
cargo nxrs build --manifest-path tests/nuttx-std/peripherals/Cargo.toml \
  --profile pico2w-peripherals
```

Use the dedicated qualification image for the board under test. The WROOM
profile reserves 64 KiB at flash offset `0xf00000`, above the WROOM MCUboot slots
and configuration partition. The Pico 2 W profile reserves its last 64 KiB at
`0x3f0000`. Back up existing firmware before flashing the qualification image. Run
`peripheral_qualification` from NSH and check for `RTC PASS`, `WATCHDOG PASS`, and
`FLASH PASS`.

The fixture sets the RTC and reads both the device and OS wall clocks. It arms,
feeds and disarms the runtime watchdog. It verifies aligned program and
sector-local erase, neighboring-sector preservation, cached read coherency and
persistence across descriptor reopen.

Run `peripheral_qualification expire` to arm the main watchdog for three seconds
and stop feeding it. Wait for NSH to return after the reset, then run
`peripheral_qualification cause` and check for `RESET CAUSE MainWatchdog`.
Time retention across power cycles is a separate test.

On the Pico 2 W, run `peripheral_qualification flash-reset-write` to write a
pattern to the scratch partition and arm the watchdog. After USB re-enumerates,
run `peripheral_qualification flash-reset-check` to verify the pattern, the
erased bytes sampled in the neighboring sector, and the reset cause, then
erase the test pattern.

## GPIO, PWM and ADC

These are the endpoints selected by the qualification images. Disconnect USB
power before adding or moving a jumper.

| Board | GPIO output | Rising-edge input | PWM output | ADC |
| --- | --- | --- | --- | --- |
| WROOM | GPIO15, `/dev/gpio0` | GPIO14, `/dev/gpio2` | GPIO14, `/dev/pwm0` | GPIO1 and GPIO2, `/dev/adc0` |
| Pico 2 W | GP6 (pin 9), `/dev/gpio6` | GP14 (pin 19), `/dev/gpio14` | GP14 (pin 19), `/dev/pwm7` | On-chip temperature, `/dev/adc0` |

- Run `peripheral_qualification io <gpio-output> <pwm-output>` without a jumper
  to check the output controls.
- On WROOM, run `peripheral_qualification gpio-readback /dev/gpio0` without a
  jumper to verify that the GPIO15 pad follows high and low writes.
- Connect GPIO output to the edge input, then run
  `peripheral_qualification gpio-loop <gpio-output> <edge-input>`.
- For PWM, remove the jumper. Run
  `peripheral_qualification pwm-loop /dev/pwm0 /dev/gpio2` on WROOM or
  `peripheral_qualification pwm-loop /dev/pwm7 /dev/gpio14` on Pico 2 W.
  Both boards sense the driven pad. The WROOM profile opts into LEDC GPIO
  readback to keep input and interrupt sensing enabled. The fixture times six
  rising-edge notifications, estimates duty before and after a duty update,
  and checks that the input stays steady after stop.
- On WROOM, run `peripheral_qualification adc-scan /dev/adc0 0 1` without
  wiring to verify channel order, replacement of an unread result on retrigger,
  and nonblocking reads of an empty FIFO. For a voltage reference check, run
  `peripheral_qualification adc /dev/adc0 trigger` with GPIO1 connected first
  to board ground and then to a measured 1 V source sharing board ground. The
  command reports channel 0 in millivolts. On Pico 2 W, run
  `peripheral_qualification adc /dev/adc0` to read the streaming on-chip
  temperature channel in driver-native units.

## Physical results

The WROOM passed the RTC, watchdog and flash checks. On expiry the ROM reported
`TG0WDT_SYS_RST` and the Rust API reported `MainWatchdog` on the same boot.
GPIO15 matched all 128 high and low writes across four opens. The two-channel
ADC fixture verified 192 triggers across three opens, including 96 complete
scans, with no stale channel records.
Three PWM runs each delivered six rising edges to the GPIO worker. The worker
observed 39–40 ms periods, estimated duty at 25% and 47% for the requested 25%
and 50%, and observed a steady pad after stop. The full 16 MiB flash was
independently verified before installation and after restoration.

The single-core, signed `home-cam` startup profile on WROOM adopted and
disarmed its inherited RTC watchdog and remained at commissioning for
190 seconds, past the 180-second budget. A signed trial confirmed after
delivering video over mutual TLS. A trial that stalled at commissioning
triggered the RTC watchdog and reverted to the signed baseline. See the
[firmware update qualification](../../../docs/firmware-updates.md)
for the reset-cause evidence.

In the earlier Pico 2 W campaign, RTC, watchdog and flash passed. Intentional
watchdog expiry reported `MainWatchdog` after reboot. The ADC returned a sample
on the on-chip temperature channel. Three no-jumper PWM runs each delivered
six rising edges to the GPIO worker. The worker observed 39–40 ms periods for
the requested 25 Hz. Task-context level samples estimated 25% and 47% duty for
the requested 25% and 50%, and the pad stayed steady after stop. The 4 MiB
flash was independently verified before installation and after restoration.

After BOOTSEL recovery, the restored Pico firmware completed two software
BOOTSEL entry and reboot cycles. A repeat with the peripheral qualification
image delivered all 18 expected GPIO notifications across three no-jumper PWM
runs, with 39–40 ms between notifications. The original 4 MiB flash was
independently verified after restoration, and the board returned to NSH.

In three Pico 2 W watchdog reset trials, NSH set a distinct UTC time and
read it again after USB re-enumerated. The wall clock advanced 15 seconds
in each trial over 15.77–15.88 seconds of host time, and `boot::reset_cause()`
reported `MainWatchdog` each time. The original 4 MiB flash was restored and
verified. Retention across removal of board power is still unverified.

Three Pico 2 W flash trials each preserved the 256 programmed bytes and the
256 erased bytes sampled in the neighboring sector through watchdog expiry.
The fixture verified the reset cause on the same reboot and erased its test
pattern. The original 4 MiB flash was restored and verified. These resets
followed completed writes.

The separate GPIO output-to-input jumper checks and the ADC reference-voltage
check remain pending physical wiring.
