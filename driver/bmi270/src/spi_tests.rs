// Independent SPI framing witness; register/FIFO behavior reuses the I2C model.
struct Spi {
    bus: Bus,
    selected: bool,
    selections: usize,
    image: Vec<u8>,
}
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
                assert_eq!(header.len(), 2, "BMI270 read requires one dummy byte");
                assert_eq!(header[0] & 0x80, 0x80);
                if !self.selected {
                    assert_eq!(header[0], 0x80);
                    self.selected = true;
                    self.selections += 1;
                    data.fill(0xff); // Selection read cannot be treated as chip identity.
                    return Ok(());
                }
                self.bus.write_read(0x68, &[header[0] & 0x7f], data)
            }
            [Op::Write(header), Op::Write(data)] => {
                assert!(self.selected);
                assert_eq!(header.len(), 1);
                assert_eq!(header[0] & 0x80, 0, "SPI writes clear the read bit");
                if header[0] == 0x5e {
                    let address = (usize::from(self.bus.registers[0x5c]) << 4
                        | usize::from(self.bus.registers[0x5b] & 15))
                        * 2;
                    assert_eq!(address, self.image.len());
                    self.image.extend_from_slice(data);
                }
                if header[0] == 0x7e && *data == [0xb6] {
                    self.selected = false;
                }
                self.bus.write(0x68, &[&header[..], &data[..]].concat())
            }
            _ => panic!("unexpected SPI transaction"),
        };
        result.map_err(|_| embedded_hal::spi::ErrorKind::Other)
    }
}

#[test]
fn spi_selects_after_reset_uploads_exact_image_and_preserves_fifo_results() {
    let spi = Spi {
        bus: Bus::new(),
        selected: false,
        selections: 0,
        image: Vec::new(),
    };
    let mut sensor = Bmi270Spi::open_spi(spi, Delay::new(), Config::default()).unwrap();
    assert_eq!(sensor.io.0.selections, 2);
    assert_eq!(
        sensor.io.0.image.as_slice(),
        bmi2::config::BMI270_CONFIG_FILE
    );
    for axis in [0_i16, 0, 16384, 8192, -8192, 0] {
        sensor.io.0.bus.fifo.extend_from_slice(&axis.to_le_bytes());
    }
    let mut sample = None;
    assert_eq!(
        sensor.read_available(|value| {
            sample = Some(value);
            true
        }),
        Ok(Some(1))
    );
    let sample = sample.unwrap();
    assert!((sample.accel_mps2[0] - 9.80665).abs() < 1e-5);
    assert!((sample.gyro_rps[2] - 17.453293).abs() < 1e-5);
    assert_eq!(
        sensor.read_available(|_| panic!("replayed FIFO")),
        Ok(Some(0))
    );
    sensor.io.0.bus.registers[0x1d] = 1;
    assert_eq!(
        sensor.read_available(|_| true),
        Err(peripheral::imu::Error::Overflow)
    );
    sensor.io.0.bus.registers[0x1d] = 0;
    sensor.io.0.bus.fifo.extend_from_slice(&[0; 12]);
    sensor.io.0.bus.fail_data = true;
    assert_eq!(
        sensor.read_available(|_| true),
        Err(peripheral::imu::Error::Io)
    );
    sensor.stop().unwrap();
    assert_eq!(sensor.io.0.bus.registers[0x7d], 0);
}
