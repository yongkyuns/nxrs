use super::map::{Position, Viewport};
use crate::communications::Zoom;
use crate::tracker::{Reading, Snapshot};
use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

pub const WINDOW: Duration = Duration::from_secs(12);
pub const SAMPLE_INTERVAL: Duration = Duration::from_millis(200);
const MOTION_POINTS: usize = 61;
pub const ROUTE_POINTS: usize = 96;
const EARTH_RADIUS_M: f64 = 6_378_137.0;

#[derive(Default, Clone, Copy, Debug, Eq, PartialEq)]
pub enum Metric {
    #[default]
    Acceleration,
    Rotation,
}

impl Metric {
    pub fn read(self, sample: imu::ImuSample) -> [f32; 3] {
        match self {
            Self::Acceleration => sample.accel_mps2,
            Self::Rotation => sample.gyro_rps,
        }
    }

    pub fn step(self) -> f32 {
        match self {
            Self::Acceleration => 10.0,
            Self::Rotation => 1.0,
        }
    }
}

struct Motion {
    time: Instant,
    sensors: [Option<imu::ImuSample>; 2],
}

struct Fix {
    latitude: f64,
    longitude: f64,
    map: Option<Position>,
    joins_previous: bool,
}

#[derive(Clone, Copy, Default)]
pub struct RoutePoint {
    pub east: f32,
    pub north: f32,
    pub joins_previous: bool,
}

pub struct History {
    motion: VecDeque<Motion>,
    fixes: VecDeque<Fix>,
    fix_sequence: Option<u64>,
    last_fix: Option<Instant>,
    zoom: Zoom,
}

impl History {
    pub fn new(zoom: Zoom) -> Self {
        Self {
            motion: VecDeque::with_capacity(MOTION_POINTS),
            fixes: VecDeque::with_capacity(ROUTE_POINTS),
            fix_sequence: None,
            last_fix: None,
            zoom,
        }
    }

    pub fn record(&mut self, snapshot: &Snapshot, now: Instant) {
        if self.motion.len() == MOTION_POINTS {
            self.motion.pop_front();
        }
        self.motion.push_back(Motion {
            time: now,
            sensors: snapshot.imu.map(|reading| match reading {
                Reading::Live(sample) => Some(sample),
                _ => None,
            }),
        });
        let Reading::Live(fix) = snapshot.gnss else {
            self.last_fix = None;
            return;
        };
        if self.fix_sequence == Some(fix.sequence) {
            return;
        }
        if self.fixes.len() == ROUTE_POINTS {
            self.fixes.pop_front();
        }
        self.fixes.push_back(Fix {
            latitude: fix.latitude_deg,
            longitude: fix.longitude_deg,
            map: Position::project(fix.latitude_deg, fix.longitude_deg, self.zoom),
            joins_previous: self
                .last_fix
                .is_some_and(|last| now.duration_since(last) < Duration::from_secs(4)),
        });
        self.fix_sequence = Some(fix.sequence);
        self.last_fix = Some(now);
    }

    pub fn motion(
        &self,
        imu: usize,
        metric: Metric,
        now: Instant,
    ) -> impl Iterator<Item = (f32, Option<[f32; 3]>)> + '_ {
        self.motion.iter().filter_map(move |point| {
            let age = now.saturating_duration_since(point.time);
            (age <= WINDOW).then(|| {
                (
                    1.0 - age.as_secs_f32() / WINDOW.as_secs_f32(),
                    point.sensors[imu].map(|sample| metric.read(sample)),
                )
            })
        })
    }

    pub fn project_viewport(
        &self,
        viewport: Viewport,
        points: &mut [RoutePoint; ROUTE_POINTS],
    ) -> usize {
        let mut count = 0;
        let mut joined = false;
        for fix in &self.fixes {
            let Some([east, north]) = fix.map.and_then(|point| viewport.project(point)) else {
                joined = false;
                continue;
            };
            points[count] = RoutePoint {
                east,
                north,
                joins_previous: joined && fix.joins_previous,
            };
            count += 1;
            joined = true;
        }
        count
    }

    pub fn project_route(&self, points: &mut [RoutePoint; ROUTE_POINTS]) -> usize {
        let (lat, lon) = self
            .fixes
            .front()
            .map_or((0.0, 0.0), |fi| (fi.latitude, fi.longitude));
        let parallel_radius = EARTH_RADIUS_M * lat.to_radians().cos();
        for (point, fix) in points.iter_mut().zip(&self.fixes) {
            *point = RoutePoint {
                east: (((fix.longitude - lon + 180.0).rem_euclid(360.0) - 180.0).to_radians()
                    * parallel_radius) as f32,
                north: ((fix.latitude - lat).to_radians() * EARTH_RADIUS_M) as f32,
                joins_previous: fix.joins_previous,
            };
        }
        self.fixes.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn route_ignores_duplicate_fixes_and_breaks_after_loss() {
        let now = Instant::now();
        let mut history = History::new(Zoom::default());
        let fix = |sequence, longitude_deg| {
            Reading::Live(gnss::GnssFix {
                sequence,
                timestamp_us: 0,
                is_capture_time: false,
                receiver_utc_ms: None,
                latitude_deg: 0.0,
                longitude_deg,
                speed_mps: 0.0,
                course_rad: None,
            })
        };
        let mut snapshot = Snapshot {
            clock: None,
            imu: [Reading::Waiting; 2],
            gnss: fix(1, 179.9999),
        };
        history.record(&snapshot, now);
        history.record(&snapshot, now + SAMPLE_INTERVAL);
        snapshot.gnss = fix(2, -179.9999);
        history.record(&snapshot, now + Duration::from_secs(1));
        snapshot.gnss = Reading::Stale;
        history.record(&snapshot, now + Duration::from_secs(4));
        snapshot.gnss = fix(3, -179.9998);
        history.record(&snapshot, now + Duration::from_secs(5));
        let mut points = [RoutePoint::default(); ROUTE_POINTS];
        assert_eq!(history.project_route(&mut points), 3);
        assert!((22.0..23.0).contains(&points[1].east));
        assert!(points[1].joins_previous);
        assert!(!points[2].joins_previous);
    }
}
