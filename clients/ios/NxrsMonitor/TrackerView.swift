import AVFoundation
import SwiftUI
import VisionKit
import NxrsMonitorCore

struct TrackerView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var tracker = TrackerBluetooth()
    @State private var key = ""
    @State private var showScanner = false
    @State private var scannerUnavailable = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    LabeledContent("Status", value: tracker.status)
                    if tracker.active {
                        Button("Disconnect") { tracker.stop() }
                    } else {
                        if DataScannerViewController.isSupported {
                            Button {
                                Task {
                                    if await AVCaptureDevice.requestAccess(for: .video) {
                                        showScanner = true
                                    } else {
                                        scannerUnavailable = true
                                    }
                                }
                            } label: {
                                Label("Scan pairing code", systemImage: "qrcode.viewfinder")
                            }
                        }
                        SecureField("Pairing key from tracker", text: $key)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button("Connect") {
                            tracker.start(key: key)
                            key = ""
                        }.disabled(key.isEmpty)
                    }
                }
                if let reading = tracker.reading {
                    Section("Motion") {
                        ForEach(reading.imu.indices, id: \.self) { index in
                            let imu = reading.imu[index]
                            LabeledContent(index == 0 ? "Onboard IMU" : "Module IMU") {
                                if let acceleration = imu.acceleration {
                                    Text(String(format: "%.2f m/s²", acceleration.reduce(0) { $0 + $1 * $1 }.squareRoot()))
                                } else {
                                    Text(imu.state.rawValue.capitalized)
                                }
                            }
                        }
                    }
                    Section("Location") {
                        LabeledContent("GNSS", value: reading.gnss.state.rawValue.capitalized)
                        if let lat = reading.gnss.latitude, let lon = reading.gnss.longitude, let speed = reading.gnss.speed {
                            LabeledContent("Coordinates", value: String(format: "%.5f, %.5f", lat, lon))
                            LabeledContent("Ground speed", value: String(format: "%.1f km/h", speed * 3.6))
                        }
                    }
                }
            }
            .navigationTitle("Motion Tracker")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active {
                tracker.setForeground(true)
            } else if phase == .background {
                tracker.setForeground(false)
                key = ""
            }
        }
        .onDisappear { tracker.stop(); key = "" }
        .sheet(isPresented: $showScanner) {
            NavigationStack {
                PairingScanner { key in
                    showScanner = false
                    if let key {
                        tracker.start(key: key)
                    } else {
                        scannerUnavailable = true
                    }
                }
                .navigationTitle("Scan tracker code")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { showScanner = false }
                    }
                }
            }
        }
        .alert("Camera unavailable", isPresented: $scannerUnavailable) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Allow camera access in iPhone Settings to scan the pairing code.")
        }
    }
}

private struct PairingScanner: UIViewControllerRepresentable {
    let finish: (String?) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(finish: finish) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])])
        scanner.delegate = context.coordinator
        Task { @MainActor [weak scanner] in
            guard let scanner else { return }
            do { try scanner.startScanning() }
            catch { context.coordinator.finish(nil) }
        }
        return scanner
    }

    func updateUIViewController(_ viewController: DataScannerViewController, context: Context) {}

    static func dismantleUIViewController(_ scanner: DataScannerViewController, coordinator: Coordinator) {
        scanner.stopScanning()
    }

    @MainActor final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let finish: (String?) -> Void
        private var finished = false

        init(finish: @escaping (String?) -> Void) { self.finish = finish }

        func dataScanner(_ scanner: DataScannerViewController, didAdd added: [RecognizedItem], allItems: [RecognizedItem]) {
            guard !finished else { return }
            for case .barcode(let barcode) in added {
                guard let value = barcode.payloadStringValue, value.hasPrefix("NXRS1:"),
                      value.utf8.count == 70 else { continue }
                finished = true
                scanner.stopScanning()
                finish(String(value.dropFirst(6)))
                return
            }
        }

        func dataScanner(_ scanner: DataScannerViewController,
                         becameUnavailableWithError error: DataScannerViewController.ScanningUnavailable) {
            guard !finished else { return }
            finished = true
            finish(nil)
        }
    }
}
