@preconcurrency import CoreBluetooth
import Combine
import Foundation
import Network
import NxrsMonitorCore

@MainActor final class TrackerBluetooth: NSObject, ObservableObject {
    @Published private(set) var status = "Disconnected"
    @Published private(set) var reading: TrackerTelemetry?
    @Published private(set) var active = false
    @Published private(set) var history = TrackerHistory()
    @Published private(set) var exporting = false
    @Published private(set) var interruptions = 0

    private struct Service {
        let uuid: CBUUID
        let ingress: CBUUID
        let egress: CBUUID
        let notifications: Bool

        init(_ version: String, notifications: Bool) {
            let prefix = "3CB1DE6E-A955-49B7-BE4F-C4572D3F34" + version
            uuid = CBUUID(string: prefix + "0")
            ingress = CBUUID(string: prefix + "1")
            egress = CBUUID(string: prefix + "2")
            self.notifications = notifications
        }
    }
    private static let dashboardService = Service("0", notifications: false)
    private static let exportService = Service("1", notifications: true)
    private static let services = [exportService.uuid, dashboardService.uuid]
    private static let loopback = NWEndpoint.Host.ipv4(.loopback)
    private enum Operation { case read, write }

    private var manager: CBCentralManager?
    private var disconnectingPeer: CBPeripheral?
    private var connectAfterDisconnect = false
    private var peripheral: CBPeripheral?
    private var requiresExport = false
    private var ingress: CBCharacteristic?
    private var egress: CBCharacteristic?
    private var listener: NWListener?
    private var relay: NWConnection?
    private var tlsClient: NWConnection?
    private var pairingKey: [UInt8]?
    private var pairedTracker: UUID?
    private var foreground = true
    private var retries = 0
    private var frames = TrackerExportFrames()
    private var capture = TrackerHistory()
    private var lastPlot: ContinuousClock.Instant?
    private var notifying = false
    private var relayBytes = 0
    private var pending: Data?
    private var operation: Operation?
    private var relaySending = false
    private var generation = UUID()
    private var relayTimer: Timer?
    private var lastReading: ContinuousClock.Instant?
    private var lastStreamData: ContinuousClock.Instant?

    func start(key: String) {
        stop()
        guard let bytes = Self.parseKey(key) else {
            status = "Enter the 64 hex digits from the tracker"
            return
        }
        pairingKey = bytes
        interruptions = 0
        active = true
        connect()
    }

    private func connect() {
        guard foreground, active, pairingKey != nil else { return }
        status = pairedTracker == nil ? "Looking for tracker" : "Reconnecting to tracker"
        if let manager {
            beginDiscovery(manager)
        } else {
            manager = CBCentralManager(delegate: self, queue: .main)
        }
        let token = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + (pairedTracker == nil ? 45 : 15)) { [weak self] in
            guard let self, self.generation == token, self.lastStreamData == nil else { return }
            self.recover("Connection timed out")
        }
    }

    private func beginDiscovery(_ central: CBCentralManager) {
        guard foreground, active, peripheral == nil, central.state == .poweredOn else { return }
        if disconnectingPeer != nil {
            connectAfterDisconnect = true
            return
        }
        if let pairedTracker, let peer = central.retrievePeripherals(withIdentifiers: [pairedTracker]).first {
            connect(central, to: peer)
        } else {
            central.scanForPeripherals(withServices: Self.services)
        }
    }

    private func connect(_ central: CBCentralManager, to peer: CBPeripheral) {
        peripheral = peer
        status = "Connecting to tracker"
        central.stopScan()
        peer.delegate = self
        central.connect(peer)
    }

    func stop(_ message: String = "Disconnected") {
        active = false
        pairingKey = nil
        pairedTracker = nil
        requiresExport = false
        retries = 0
        retireConnection()
        status = message
    }

    func setForeground(_ value: Bool) {
        guard foreground != value else { return }
        foreground = value
        guard active else { return }
        if value {
            retries = 0
            connect()
        } else {
            retireConnection()
            status = "Paused"
        }
    }

    private func recover(_ message: String) {
        guard pairedTracker != nil, retries < 3 else {
            stop(message)
            return
        }
        interruptions += 1
        retireConnection()
        status = "Reconnecting to tracker"
        let delay = 1 << retries
        retries += 1
        let token = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(delay)) { [weak self] in
            guard let self, self.generation == token else { return }
            self.connect()
        }
    }

    private func retireConnection() {
        generation = UUID()
        reading = nil
        lastReading = nil
        lastStreamData = nil
        relayTimer?.invalidate()
        relayTimer = nil
        manager?.stopScan()
        let peer = peripheral
        peripheral = nil
        connectAfterDisconnect = false
        if let peer {
            peer.delegate = nil
            if peer.state != .disconnected {
                disconnectingPeer = peer
                manager?.cancelPeripheralConnection(peer)
            }
        }
        ingress = nil
        egress = nil
        listener?.cancel()
        listener = nil
        relay?.cancel()
        relay = nil
        tlsClient?.cancel()
        tlsClient = nil
        pending = nil
        operation = nil
        relaySending = false
        notifying = false
        relayBytes = 0
        frames = TrackerExportFrames()
        capture = TrackerHistory()
        history = TrackerHistory()
        lastPlot = nil
        exporting = false
    }

    private static func parseKey(_ value: String) -> [UInt8]? {
        let chars = Array(value.utf8.filter { ![UInt8(32), 10, 13, 9].contains($0) })
        guard chars.count == 64 else { return nil }
        var output: [UInt8] = []
        for index in stride(from: 0, to: chars.count, by: 2) {
            guard let byte = UInt8(String(decoding: chars[index..<index+2], as: UTF8.self), radix: 16) else {
                return nil
            }
            output.append(byte)
        }
        return output.contains(where: { $0 != 0 }) ? output : nil
    }

    private func establishRelay() {
        let token = generation
        let settings = NWParameters.tcp
        settings.requiredLocalEndpoint = .hostPort(host: Self.loopback, port: .any)
        do {
            let listener = try NWListener(using: settings)
            self.listener = listener
            listener.newConnectionHandler = { [weak self] conn in
                MainActor.assumeIsolated {
                    guard let self, self.generation == token, self.relay == nil else {
                        conn.cancel()
                        return
                    }
                    self.relay = conn
                    conn.start(queue: .main)
                    self.receiveCiphertext(conn, token: token)
                }
            }
            listener.stateUpdateHandler = { [weak self, weak listener] state in
                MainActor.assumeIsolated {
                    guard let self, self.generation == token else { return }
                    switch state {
                    case .ready:
                        guard let port = listener?.port, let key = self.pairingKey else {
                            self.stop("Could not start secure connection")
                            return
                        }
                        let options = NWProtocolTLS.Options()
                        key.withUnsafeBufferPointer {
                            tracker_tls_pairing(options.securityProtocolOptions, $0.baseAddress!)
                        }
                        self.connectTLS(port: port, options: options, token: token)
                        self.startRelayTimer(token: token)
                    case .failed: self.stop("Could not start secure connection")
                    default: break
                    }
                }
            }
            listener.start(queue: .main)
        } catch {
            stop("Could not start secure connection")
        }
    }

    private func startRelayTimer(token: UUID) {
        let timer = Timer(timeInterval: 0.015, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                if !self.refreshTelemetry(at: .now) {
                    self.recover("Tracker data stopped")
                    return
                }
                self.pump()
            }
        }
        relayTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    /// Returns false after the authenticated stream stops delivering data.
    func refreshTelemetry(at now: ContinuousClock.Instant) -> Bool {
        if let last = lastStreamData, last.duration(to: now) > .seconds(12) {
            return false
        }
        if let last = lastReading, last.duration(to: now) > .seconds(4), reading != nil {
            reading = nil
            if !notifying { status = "Waiting for tracker data" }
        }
        return true
    }

    private func connectTLS(port: NWEndpoint.Port, options: NWProtocolTLS.Options, token: UUID) {
        status = "Authenticating tracker"
        let client = NWConnection(host: Self.loopback, port: port, using: NWParameters(tls: options))
        tlsClient = client
        client.stateUpdateHandler = { [weak self, weak client] state in
            MainActor.assumeIsolated {
                guard let self, let client, self.generation == token else { return }
                switch state {
                case .ready:
                    guard let metadata = client.metadata(definition: NWProtocolTLS.definition) as? NWProtocolTLS.Metadata,
                          tracker_tls_authenticated(metadata.securityProtocolMetadata)
                    else {
                        self.stop("Tracker authentication failed")
                        return
                    }
                    self.pairedTracker = self.peripheral?.identifier
                    self.status = self.notifying ? "Secure connection" : "Dashboard only"
                    if self.notifying {
                        self.exporting = true
                        client.send(content: TrackerExportFrames.request, completion: .contentProcessed { [weak self] error in
                            MainActor.assumeIsolated {
                                guard let self, self.generation == token, error != nil else { return }
                                self.recover("Could not request sensor export")
                            }
                        })
                    }
                    self.receiveTelemetry(client, token: token)
                case .failed: self.recover("Tracker authentication or connection failed")
                default: break
                }
            }
        }
        client.start(queue: .main)
    }

    private func receiveTelemetry(_ client: NWConnection, token: UUID) {
        client.receive(minimumIncompleteLength: 1, maximumLength: 1024) { [weak self, weak client] data, _, done, error in
            MainActor.assumeIsolated {
                guard let self, let client, self.generation == token else { return }
                if done || error != nil { self.recover("Secure connection closed"); return }
                if let data, !self.receiveAuthenticatedData(data, at: .now) { return }
                self.receiveTelemetry(client, token: token)
            }
        }
    }

    /// Applies the plaintext read from the authenticated tracker connection.
    func receiveAuthenticatedData(_ data: Data, at now: ContinuousClock.Instant) -> Bool {
        do {
            var newest: TrackerTelemetry?
            var measurements: [TrackerMeasurement] = []
            try frames.receive(data, snapshot: { newest = $0 }, measurement: { measurements.append($0) })
            if !measurements.isEmpty || newest != nil {
                lastStreamData = now
                retries = 0
            }
            if !measurements.isEmpty {
                for item in measurements { capture.append(item) }
                capture.trim()
                if lastPlot.map({ $0.duration(to: now) >= .milliseconds(100) }) ?? true {
                    lastPlot = now
                    history = capture
                }
            }
            if let newest {
                reading = newest
                lastReading = now
            }
            if capture.hasGaps {
                status = "Sensor export incomplete. Samples missing."
            } else if newest != nil {
                status = notifying ? "Secure connection" : "Dashboard only"
            }
            return true
        } catch TrackerFrames.FrameError.captureOverflow {
            stop("Sensor export incomplete. Tracker buffer filled.")
        } catch {
            stop("Invalid tracker telemetry")
        }
        return false
    }

    private func receiveCiphertext(_ conn: NWConnection, token: UUID) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 20) { [weak self] data, _, done, error in
            MainActor.assumeIsolated {
                guard let self, self.generation == token else { return }
                guard !done, error == nil, let data, !data.isEmpty else {
                    self.recover("Secure connection closed")
                    return
                }
                self.pending = data
                self.pump()
            }
        }
    }

    private func pump() {
        guard let peripheral, let ingress, let egress, relay != nil, operation == nil, !relaySending else {
            return
        }
        if let pending {
            operation = .write
            peripheral.writeValue(pending, for: ingress, type: .withResponse)
        } else if !notifying {
            operation = .read
            peripheral.readValue(for: egress)
        }
    }
}

extension TrackerBluetooth: @preconcurrency CBCentralManagerDelegate, @preconcurrency CBPeripheralDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard central === manager else { return }
        switch central.state {
        case .poweredOn: beginDiscovery(central)
        case .unknown, .resetting: break
        case .poweredOff:
            disconnectingPeer = nil
            if active { recover("Bluetooth is unavailable") }
        default: stop("Bluetooth is unavailable")
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard central === manager, foreground, active, self.peripheral == nil, disconnectingPeer == nil,
              pairedTracker == nil || peripheral.identifier == pairedTracker else { return }
        if (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID])?.contains(Self.exportService.uuid) == true {
            requiresExport = true
        }
        connect(central, to: peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard central === manager, peripheral === self.peripheral else { return }
        status = "Reading tracker services"
        peripheral.discoverServices(Self.services)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        connectionEnded(central, peripheral, message: "Tracker disconnected")
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        connectionEnded(central, peripheral, message: "Could not reach tracker")
    }

    private func connectionEnded(_ central: CBCentralManager, _ peer: CBPeripheral, message: String) {
        guard central === manager else { return }
        if peer === disconnectingPeer {
            disconnectingPeer = nil
            if connectAfterDisconnect {
                connectAfterDisconnect = false
                beginDiscovery(central)
            }
        } else if peer === peripheral {
            recover(message)
        }
    }

    func peripheral(_ peer: CBPeripheral, didDiscoverServices error: Error?) {
        guard peer === peripheral else { return }
        guard error == nil, let offered = peer.services else {
            stop("Tracker service unavailable")
            return
        }
        if let service = offered.first(where: { $0.uuid == Self.exportService.uuid }) {
            requiresExport = true
            peer.discoverCharacteristics([Self.exportService.ingress, Self.exportService.egress], for: service)
        } else if !requiresExport, let service = offered.first(where: { $0.uuid == Self.dashboardService.uuid }) {
            peer.discoverCharacteristics([Self.dashboardService.ingress, Self.dashboardService.egress], for: service)
        } else {
            stop("Tracker service unavailable")
        }
    }

    func peripheral(_ peer: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard peer === peripheral, listener == nil else { return }
        let profile = service.uuid == Self.exportService.uuid ? Self.exportService : Self.dashboardService
        ingress = service.characteristics?.first { $0.uuid == profile.ingress && $0.properties.contains(.write) }
        egress = service.characteristics?.first {
            $0.uuid == profile.egress && $0.properties.contains(profile.notifications ? .notify : .read)
        }
        guard error == nil, ingress != nil, egress != nil,
              peer.maximumWriteValueLength(for: .withResponse) >= 20 else {
            stop("Tracker transport unavailable")
            return
        }
        if let egress, profile.notifications {
            peer.setNotifyValue(true, for: egress)
        } else {
            establishRelay()
        }
    }

    func peripheral(_ peer: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard peer === peripheral, characteristic === egress else { return }
        if listener == nil {
            guard error == nil && characteristic.isNotifying else {
                recover("Could not subscribe to sensor export")
                return
            }
            notifying = true
            establishRelay()
        } else if notifying && (error != nil || !characteristic.isNotifying) {
            recover("Tracker notifications stopped")
        }
    }

    func peripheral(_ peer: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard peer === peripheral, characteristic === ingress, operation == .write else { return }
        guard error == nil, let relay else { recover("Bluetooth write failed"); return }
        operation = nil
        pending = nil
        receiveCiphertext(relay, token: generation)
        // The periodic read gives the tracker a chance to drain outgoing ciphertext.
    }

    func peripheral(_ peer: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard peer === peripheral, characteristic === egress, notifying || operation == .read else { return }
        guard error == nil, let data = characteristic.value,
              data.count <= (notifying ? 244 : 20), let relay else {
            recover("Bluetooth data failed")
            return
        }
        if !notifying { operation = nil }
        guard !data.isEmpty else { return }
        guard relayBytes + data.count <= 8192 else {
            recover("Bluetooth receiver fell behind")
            return
        }
        relayBytes += data.count
        relaySending = !notifying
        let token = generation
        relay.send(content: data, completion: .contentProcessed { [weak self] error in
            MainActor.assumeIsolated {
                guard let self, self.generation == token else { return }
                self.relayBytes -= data.count
                self.relaySending = false
                if error != nil { self.recover("Secure connection closed") }
            }
        })
    }
}
