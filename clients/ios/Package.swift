// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NxrsMonitorCore",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "NxrsMonitorCore", targets: ["NxrsMonitorCore"])],
    targets: [
        .target(name: "NxrsMonitorCore"),
        .testTarget(name: "NxrsMonitorCoreTests", dependencies: ["NxrsMonitorCore"],
                    resources: [.copy("Fixtures")])
    ]
)
