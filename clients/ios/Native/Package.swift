// swift-tools-version: 5.9
import PackageDescription

// Community binary distribution, pinned URL + SHA256. See THIRD_PARTY.md.
// The dependency-free core remains testable without downloading Apple binaries.
let package = Package(
    name: "NxrsMonitorNative",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "NxrsMonitorNative", targets: ["NxrsMonitorNative"]),
        .executable(name: "NativePlaybackProbe", targets: ["NativePlaybackProbe"]),
        .executable(name: "DetectionProbe", targets: ["DetectionProbe"])
    ],
    dependencies: [.package(name: "NxrsMonitorCore", path: "..")],
    targets: [
        .binaryTarget(name: "WebRTC",
            url: "https://github.com/stasel/WebRTC/releases/download/155.0.0/WebRTC-M155.xcframework.zip",
            checksum: "cb9adacf2d6bbf598d1d9e3e6c16d8fb97ca987b5b697746891828f9dc068ffe"),
        .target(name: "NxrsMonitorNative", dependencies: ["WebRTC",
            .product(name: "NxrsMonitorCore", package: "NxrsMonitorCore")]),
        .executableTarget(name: "NativePlaybackProbe", dependencies: ["NxrsMonitorNative",
            .product(name: "NxrsMonitorCore", package: "NxrsMonitorCore")]),
        .executableTarget(name: "DetectionProbe", dependencies: [
            .product(name: "NxrsMonitorCore", package: "NxrsMonitorCore")]),
        .testTarget(name: "NxrsMonitorNativeTests", dependencies: ["NxrsMonitorNative"])
    ]
)
