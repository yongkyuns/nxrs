// Independent wire framing, reusing the existing register/FIFO model.
struct Spi(Bus);
impl embedded_hal::spi::ErrorType for Spi {
    type Error = embedded_hal::spi::ErrorKind;
}
impl SpiDevice for Spi {
    fn transaction(
        &mut self,
        ops: &mut [embedded_hal::spi::Operation<'_, u8>],
    ) -> Result<(), Self::Error> {
        use embedded_hal::spi::Operation as Op;
        let result = match ops {
            [Op::Write(header), Op::Read(data)] => {
                assert_eq!(header.len(), 1, "LSM6DSO has no BMI270 dummy byte");
                assert_eq!(header[0] & 0x80, 0x80);
                self.0.write_read(0x6a, &[header[0] & 0x7f], data)
            }
            [Op::Write(header), Op::Write(data)] => {
                assert_eq!(header.len(), 1);
                assert_eq!(header[0] & 0x80, 0);
                self.0.write(0x6a, &[&header[..], &data[..]].concat())
            }
            _ => panic!("unexpected SPI transaction"),
        };
        result.map_err(|_| embedded_hal::spi::ErrorKind::Other)
    }
}

#[test]
fn spi_preserves_tagged_fifo_units_overflow_and_shutdown_errors() {
    let mut sensor = Lsm6dsoSpi::open_spi(Spi(Bus::new()), Delay, Config::default()).unwrap();
    sensor.io.0 .0.push(1, 0, [16384, 0, 0]);
    sensor.io.0 .0.push(2, 0, [8192, 0, 0]);
    let mut sample = None;
    assert_eq!(
        sensor.read_available(|value| {
            sample = Some(value);
            true
        }),
        Ok(Some(1))
    );
    let sample = sample.unwrap();
    assert!((sample.accel_mps2[0] - 9.80665 * 0.999424).abs() < 0.001);
    assert!((sample.gyro_rps[0] - 16384.0 * 0.070_f32.to_radians()).abs() < 0.001);
    assert_eq!(
        sensor.read_available(|_| panic!("replayed FIFO")),
        Ok(Some(0))
    );
    sensor.io.0 .0.status_flags = 0x40;
    assert_eq!(
        sensor.read_available(|_| true),
        Err(peripheral::imu::Error::Overflow)
    );
    sensor.io.0 .0.failed_write = Some(CTRL1_XL);
    assert_eq!(sensor.stop(), Err(Error::Bus));
    sensor.io.0 .0.failed_write = None;
    sensor.stop().unwrap();
    assert_eq!(&sensor.io.0 .0.regs[0x10..=0x11], &[0, 0]);
}
