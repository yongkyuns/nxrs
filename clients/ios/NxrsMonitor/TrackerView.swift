import AVFoundation
import MapKit
import SwiftUI
import VisionKit
import NxrsMonitorCore

struct TrackerView: View {
    let connectionChanged: (Bool) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var tracker = TrackerBluetooth()
    @State private var key = ""
    @State private var showScanner = false
    @State private var scannerUnavailable = false
    @State private var mode = MotionGraph.Quantity.acceleration

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
                if tracker.exporting {
                    Section("Sensor export") {
                        if tracker.history.hasData {
                            Picker("Measurement", selection: $mode) {
                                ForEach(MotionGraph.Quantity.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                            }
                            .pickerStyle(.segmented)
                            ForEach(0..<2, id: \.self) { index in
                                VStack(alignment: .leading, spacing: 8) {
                                    Text(index == 0 ? "Onboard IMU" : "Module IMU").font(.headline)
                                    MotionGraph(samples: tracker.history.motion[index], quantity: mode)
                                    delivery(tracker.history.counts[index])
                                }
                                .padding(.vertical, 4)
                            }
                            LabeledContent("GNSS") { delivery(tracker.history.counts[2]) }
                            if tracker.interruptions != 0 {
                                LabeledContent("Reconnects", value: tracker.interruptions.formatted())
                            }
                        } else {
                            Text("Waiting for full-rate sensor data").foregroundStyle(.secondary)
                        }
                    }
                }
                if let reading = tracker.reading {
                    if !tracker.history.hasData { Section("Motion") {
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
                    }
                    Section("Location") {
                        if let lat = reading.gnss.latitude, let lon = reading.gnss.longitude {
                            TrackerMap(coordinate: .init(latitude: lat, longitude: lon),
                                       positions: tracker.history.positions)
                                .frame(height: 240)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                                .accessibilityLabel("Tracker location and recent route")
                        }
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
        .onChange(of: tracker.active, perform: connectionChanged)
        .onDisappear {
            tracker.stop()
            connectionChanged(false)
            key = ""
        }
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

    private func delivery(_ count: TrackerHistory.Count) -> some View {
        let rate = count.rate.map { String(format: "%.1f Hz · ", $0) } ?? ""
        return Text("\(rate)\(count.received.formatted()) received · \(count.dropped.formatted()) missed")
            .font(.caption.monospacedDigit())
            .foregroundStyle(count.dropped == 0 ? Color.secondary : Color.orange)
    }
}

private struct MotionGraph: View {
    enum Quantity: String, CaseIterable {
        case acceleration = "Acceleration"
        case gyro = "Gyroscope"
        var unit: String { self == .acceleration ? "m/s²" : "rad/s" }
    }

    let samples: [TrackerHistory.Motion]
    let quantity: Quantity
    private let colors: [Color] = [.red, .green, .blue]

    var body: some View {
        if let newest = samples.last {
            let axes = samples.map { quantity == .acceleration ? $0.acceleration : $0.gyro }
            let limit = max(0.1, axes.flatMap { $0 }.map { abs($0) }.max() ?? 0)
            Canvas { context, size in
                let mid = size.height / 2
                var zero = Path()
                zero.move(to: CGPoint(x: 0, y: mid))
                zero.addLine(to: CGPoint(x: size.width, y: mid))
                context.stroke(zero, with: .color(.secondary.opacity(0.4)))
                for axis in colors.indices {
                    var line = Path()
                    var previous: UInt32?
                    for (sample, values) in zip(samples, axes) {
                        let ago = newest.sequence &- sample.sequence
                        guard ago < 500 else { continue }
                        let point = CGPoint(x: size.width * (1 - CGFloat(ago) / 500),
                                            y: mid - CGFloat(values[axis] / limit) * (mid - 5))
                        if let previous, sample.sequence &- previous == 1 { line.addLine(to: point) }
                        else { line.move(to: point) }
                        previous = sample.sequence
                    }
                    context.stroke(line, with: .color(colors[axis]), lineWidth: 1.3)
                }
            }
            .frame(height: 96)
            .accessibilityLabel("Recent \(quantity.rawValue.lowercased()) across three axes")
            HStack(spacing: 12) {
                ForEach(0..<3, id: \.self) { axis in
                    Label(["X", "Y", "Z"][axis], systemImage: "circle.fill")
                        .labelStyle(.titleAndIcon)
                        .foregroundStyle(colors[axis])
                }
                Spacer()
                Text("Last 5 s · ±\(limit.formatted(.number.precision(.fractionLength(1)))) \(quantity.unit)")
                    .foregroundStyle(.secondary)
            }
            .font(.caption2)
        } else {
            Text("Waiting for samples").foregroundStyle(.secondary)
        }
    }
}

private struct TrackerMap: UIViewRepresentable {
    let coordinate: CLLocationCoordinate2D
    let positions: [TrackerHistory.Position]

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        context.coordinator.pin.coordinate = coordinate
        map.addAnnotation(context.coordinator.pin)
        map.setRegion(MKCoordinateRegion(center: coordinate,
                                        latitudinalMeters: 500, longitudinalMeters: 500), animated: false)
        return map
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        let coordinator = context.coordinator
        coordinator.pin.coordinate = coordinate
        guard positions.last?.sequence != coordinator.lastSequence else { return }
        coordinator.lastSequence = positions.last?.sequence
        map.removeOverlays(map.overlays)
        var segment: [CLLocationCoordinate2D] = []
        var previous: UInt32?
        for point in positions {
            if let previous, point.sequence &- previous != 1 {
                if segment.count > 1 { map.addOverlay(MKPolyline(coordinates: segment, count: segment.count)) }
                segment.removeAll(keepingCapacity: true)
            }
            segment.append(.init(latitude: point.latitude, longitude: point.longitude))
            previous = point.sequence
        }
        if segment.count > 1 { map.addOverlay(MKPolyline(coordinates: segment, count: segment.count)) }
    }

    final class Coordinator: NSObject, MKMapViewDelegate {
        let pin = MKPointAnnotation()
        var lastSequence: UInt32?
        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            guard let polyline = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let line = MKPolylineRenderer(polyline: polyline)
            line.strokeColor = .systemBlue
            line.lineWidth = 3
            return line
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
