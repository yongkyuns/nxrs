use super::{
    canvas::CanvasExt,
    dashboard::{GOLD, GRID, MUTED, RED, TEXT},
};
use crate::{text::Text, tracker::Reading};
use display::graphics::{Area, Raster};
use gnss::{Constellation, FixQuality, GnssFix, SatelliteReport, SatelliteSignal};
use std::fmt::Write;

const PER_ROW: usize = 12;

#[derive(PartialEq)]
pub(super) struct View {
    fix: Reading<Option<FixQuality>>,
    satellites: Reading<SatelliteReport>,
}

impl View {
    pub(super) fn new(fix: Reading<GnssFix>, satellites: Reading<SatelliteReport>) -> Self {
        let fix = match fix {
            Reading::Live(fix) => Reading::Live(fix.quality.map(|quality| FixQuality {
                satellites: None,
                ..quality
            })),
            Reading::Waiting => Reading::Waiting,
            Reading::Stale => Reading::Stale,
            Reading::Fault => Reading::Fault,
        };
        Self { fix, satellites }
    }

    pub(super) fn paint(&self, canvas: &mut Raster<'_>, width: i32, height: i32, top: i32) {
        canvas.text(12, top + 6, b"GNSS SIGNAL", 2, TEXT);
        if canvas.intersects_rows(top + 26, 29) {
            self.fix_metrics(canvas, top + 26);
        }
        let Reading::Live(report) = &self.satellites else {
            let (label, color): (&[u8], _) = match self.satellites {
                Reading::Waiting => (b"WAITING FOR SATELLITE REPORT", GOLD),
                Reading::Stale => (b"SATELLITE REPORT STALE", GOLD),
                Reading::Fault => (b"GNSS SENSOR FAULT", RED),
                Reading::Live(_) => unreachable!(),
            };
            canvas.text(12, top + 64, label, 1, color);
            return;
        };
        let signals = report.signals();
        if canvas.intersects_rows(top + 62, 7) {
            let mut line = Text::<36>::new();
            let _ = write!(line, "VIS {}  TRACK {}", report.visible, report.tracked);
            if let Some(mean) = report.mean_cn0_db_hz {
                let _ = write!(line, "   AVG {mean} DB-HZ");
            }
            canvas.text(12, top + 62, line.bytes(), 1, TEXT);
        }
        if signals.is_empty() {
            canvas.text(12, top + 78, b"NO SIGNAL MEASUREMENTS", 1, GOLD);
            return;
        }
        if canvas.intersects_rows(top + 76, 7) {
            canvas.text(12, top + 76, b"C/N0 DB-HZ", 1, MUTED);
            if usize::from(report.measured) > signals.len() {
                let mut count = Text::<19>::new();
                let _ = write!(count, "TOP {} OF {}", signals.len(), report.measured);
                canvas.text(
                    width - 12 - count.bytes().len() as i32 * 6,
                    top + 76,
                    count.bytes(),
                    1,
                    MUTED,
                );
            }
        }
        let chart_y = top + 87;
        let row_height = (height - 22 - chart_y) / signals.len().div_ceil(PER_ROW) as i32;
        let peak = signals
            .iter()
            .map(|signal| u16::from(signal.cn0_db_hz))
            .max()
            .unwrap_or(0);
        let max = i32::from(peak.max(10).div_ceil(10) * 10);
        for (row, signals) in signals.chunks(PER_ROW).enumerate() {
            let y = chart_y + row as i32 * row_height;
            if canvas.intersects_rows(y, row_height) {
                bars(canvas, width, y, row_height - 12, max, signals);
            }
        }
        legend(canvas, height - 12);
    }

    fn fix_metrics(&self, canvas: &mut Raster<'_>, y: i32) {
        let Reading::Live(Some(fix)) = self.fix else {
            let (text, color): (&[u8], _) = match self.fix {
                Reading::Waiting => (b"WAITING FOR FIX", GOLD),
                Reading::Stale => (b"FIX STALE", GOLD),
                Reading::Fault => (b"FIX UNAVAILABLE", RED),
                Reading::Live(None) => (b"FIX QUALITY UNAVAILABLE", MUTED),
                Reading::Live(Some(_)) => unreachable!(),
            };
            canvas.text(12, y, text, 1, color);
            return;
        };
        let mut position = Text::<41>::new();
        let _ = write!(
            position,
            "H ACC {:.1} M  V ACC {:.1} M",
            fix.horizontal_accuracy_m, fix.vertical_accuracy_m
        );
        canvas.text(12, y, position.bytes(), 1, TEXT);
        let mut speed = Text::<36>::new();
        let _ = write!(speed, "S ACC {:.2} M/S  PDOP ", fix.speed_accuracy_mps);
        if let Some(pdop) = fix.position_dop {
            let _ = write!(speed, "{pdop:.2}");
        } else {
            let _ = write!(speed, "--");
        }
        canvas.text(12, y + 11, speed.bytes(), 1, TEXT);
        let mut used = Text::<9>::new();
        let _ = write!(used, "USED {}", fix.satellites_used);
        canvas.text(12, y + 22, used.bytes(), 1, TEXT);
    }
}

fn bars(
    canvas: &mut Raster<'_>,
    width: i32,
    y: i32,
    height: i32,
    max: i32,
    signals: &[SatelliteSignal],
) {
    let slot = (width - 42) / PER_ROW as i32;
    let bottom = y + height;
    for (value, line) in [(max, y), (max / 2, y + height / 2), (0, bottom)] {
        canvas.fill(
            Area {
                x: 32,
                y: line,
                w: width - 43,
                h: 1,
            },
            GRID,
        );
        let mut label = Text::<3>::new();
        let _ = write!(label, "{value}");
        canvas.text(
            7,
            if value == max { line } else { line - 3 },
            label.bytes(),
            1,
            MUTED,
        );
    }
    for (index, signal) in signals.iter().enumerate() {
        let color = series(signal.constellation).1;
        let x = 32 + index as i32 * slot;
        let h = (i32::from(signal.cn0_db_hz) * height / max).max(1);
        canvas.fill(
            Area {
                x: x + 3,
                y: bottom - h,
                w: slot - 6,
                h,
            },
            color,
        );
        if canvas.intersects_rows(bottom + 3, 7) {
            let mut label = Text::<3>::new();
            let _ = write!(label, "{}", signal.id);
            canvas.text(
                x + (slot - label.bytes().len() as i32 * 6) / 2,
                bottom + 3,
                label.bytes(),
                1,
                color,
            );
        }
    }
}

const SYSTEMS: [Constellation; 8] = [
    Constellation::Gps,
    Constellation::Galileo,
    Constellation::Beidou,
    Constellation::Glonass,
    Constellation::Sbas,
    Constellation::Qzss,
    Constellation::Navic,
    Constellation::Other,
];

fn series(system: Constellation) -> (&'static [u8], u16) {
    match system {
        Constellation::Gps => (b"GPS", 0x57f4),
        Constellation::Galileo => (b"GAL", 0x661f),
        Constellation::Beidou => (b"BDS", 0xff06),
        Constellation::Glonass => (b"GLO", 0xfb0d),
        Constellation::Sbas => (b"SBAS", 0xbc5f),
        Constellation::Qzss => (b"QZSS", 0x4fdf),
        Constellation::Navic => (b"NAVIC", 0xfc70),
        Constellation::Other => (b"OTHER", 0xbdf7),
    }
}

fn legend(canvas: &mut Raster<'_>, y: i32) {
    if canvas.intersects_rows(y, 7) {
        let mut x = 12;
        for system in SYSTEMS {
            let (label, color) = series(system);
            canvas.text(x, y, label, 1, color);
            x += label.len() as i32 * 6 + 8;
        }
    }
}
