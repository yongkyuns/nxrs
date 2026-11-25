// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NxrsMonitorCore",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "NxrsMonitorCore", targets: ["NxrsMonitorCore"]),
        .library(name: "NxrsTrackerCore", targets: ["NxrsTrackerCore"])
    ],
    targets: [
        .target(name: "NxrsMonitorCore"),
        .target(name: "NxrsTrackerCore"),
        .testTarget(name: "NxrsTrackerCoreTests", dependencies: ["NxrsTrackerCore"]),
        .testTarget(name: "NxrsMonitorCoreTests", dependencies: ["NxrsMonitorCore"],
                    resources: [.copy("Fixtures")])
    ]
)
