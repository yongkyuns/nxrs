//! Motion Tracker identity and traffic budgets for reusable telemetry delivery.
use ble::{peripheral::Service, Uuid};
use telemetry::{
    BleServices, CaptureBudget, CaptureConfig, CellularConfig, Config, Endpoint, Names,
};

pub fn config(
    pair_on_start: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
) -> Config {
    let service = |base| Service {
        uuid: uuid(base),
        ingress: uuid(base + 1),
        egress: uuid(base + 2),
    };
    Config {
        ble: BleServices {
            pairing: service(0),
            telemetry: service(0x20),
        },
        pair_on_start,
        cellular,
        tile_relay,
        names: Names {
            product: "motion-tracker",
            worker: "tracker-links",
            collector_worker: "tracker-collector",
            tile_worker: "tracker-tile-dial",
        },
    }
}

fn uuid(last: u8) -> Uuid {
    Uuid([
        0x3c, 0xb1, 0xde, 0x6e, 0xa9, 0x55, 0x49, 0xb7, 0xbe, 0x4f, 0xc4, 0x57, 0x2d, 0x3f, 0x34,
        last,
    ])
}

pub fn capture(
    scales: [Option<[u32; 2]>; 2],
    imu_rates_millihz: [Option<u32>; 2],
    reserve_export: bool,
) -> CaptureConfig {
    CaptureConfig {
        scales,
        imu_rates_millihz,
        // The selected product profiles configure a 500 ms navigation period.
        // This is a product assumption, not a rate discovered by the GNSS HAL.
        gnss_rate_millihz: 2_000,
        reserve_export,
        budget: CaptureBudget {
            max_requested_bytes: 22 * 1024,
            stall_ms: 2_000,
            burst_records: 64,
        },
    }
}
