#![cfg_attr(not(test), no_std)]

#[path = "../../service-footprint/src/packet_service.rs"]
pub mod packet_service;
#[path = "../../service-footprint/src/payload_kernels.rs"]
pub mod payload_kernels;

pub mod messaging;
pub mod protocol;
