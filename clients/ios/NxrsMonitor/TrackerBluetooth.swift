@preconcurrency import CoreBluetooth
import Combine
import Foundation
import Network
import NxrsMonitorCore

struct TrackerNotificationStream {
    private(set) var offset: UInt32 = 0

    var acknowledgement: Data {
        var littleEndian = offset.littleEndian
        return withUnsafeBytes(of: &littleEndian) { Data([1]) + Data($0) }
    }

    mutating func accept(_ packet: Data) -> Data? {
        guard packet.count > 4 else { return nil }
        let start = packet.prefix(4).enumerated().reduce(UInt32(0)) { result, byte in
            result | (UInt32(byte.element) << (8 * byte.offset))
        }
        let size = UInt32(packet.count - 4)
        let skip = offset &- start
        // Ignore future and duplicate bytes. The tracker replays from our
        // last contiguous offset, which may arrive in a different chunk size.
        guard skip < size else { return nil }
        offset &+= size - skip
        return Data(packet.dropFirst(4 + Int(skip)))
    }
}

struct TrackerPresentation: Sendable {
    var status = "Disconnected"
    var reading: TrackerTelemetry?
    var active = false
    var history = TrackerHistory()
    var exporting = false
    var interruptions = 0
}

@MainActor final class TrackerBluetooth: ObservableObject {
    @Published private var presentation = TrackerPresentation()
    private lazy var connection = TrackerConnection { [weak self] snapshot in
        self?.presentation = snapshot
    }

    var status: String { presentation.status }
    var reading: TrackerTelemetry? { presentation.reading }
    var active: Bool { presentation.active }
    var history: TrackerHistory { presentation.history }
    var exporting: Bool { presentation.exporting }
    var interruptions: Int { presentation.interruptions }

    func start(key: String) {
        connection.dispatch { $0.start(key: key) }
    }

    func stop() {
        connection.dispatch { $0.stop() }
    }

    func setForeground(_ foreground: Bool) {
        connection.dispatch { $0.setForeground(foreground) }
    }
}

/// A single latest snapshot crosses to the UI. A slow main queue can't retain
/// a backlog of graph histories or delay the next Bluetooth acknowledgment.
private final class TrackerUpdates: @unchecked Sendable {
    private let lock = NSLock()
    private var pending: TrackerPresentation?
    private let present: @MainActor @Sendable (TrackerPresentation) -> Void

    init(present: @escaping @MainActor @Sendable (TrackerPresentation) -> Void) {
        self.present = present
    }

    func send(_ snapshot: TrackerPresentation) {
        let schedule = lock.withLock {
            let idle = pending == nil
            pending = snapshot
            return idle
        }
        if schedule {
            DispatchQueue.main.async { [self] in
                let snapshot = lock.withLock {
                    defer { pending = nil }
                    return pending
                }
                if let snapshot { present(snapshot) }
            }
        }
    }
}

/// All transport and decoding state is confined to the same serial queue.
/// CoreBluetooth and Network framework deliver their callbacks on that queue.
final class TrackerConnection: NSObject, @unchecked Sendable {
    private let queue = DispatchQueue(label: "org.nxrs.tracker.bluetooth", qos: .userInitiated)
    private let updates: TrackerUpdates
    private(set) var presentation = TrackerPresentation() {
        didSet { updates.send(presentation) }
    }

    init(present: @escaping @MainActor @Sendable (TrackerPresentation) -> Void) {
        updates = TrackerUpdates(present: present)
    }

    func dispatch(_ operation: @escaping @Sendable (TrackerConnection) -> Void) {
        queue.async { [self] in operation(self) }
    }

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
    private static let exportService = Service("2", notifications: true)
    private static let services = [exportService.uuid, dashboardService.uuid]
    private static let loopback = NWEndpoint.Host.ipv4(.loopback)
    private enum Operation: Equatable { case read, write, probe(payload: Int) }

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
    private var requestedExport = false
    private var notificationStream = TrackerNotificationStream()
    private var acknowledgementPending = false
    private var relayBytes = 0
    private var pending: Data?
    private var operation: Operation?
    private var ingressPayload = 19
    private var writesSinceRead = 0
    private var writeRetries = 0
    private var writeDeadline: ContinuousClock.Instant?
    private var wantedTile: TrackerTile?
    private var offeredTile: TrackerTile?
    private var tileTask: Task<Void, Never>?
    private var relaySending = false
    private var generation = UUID()
    private var relayTimer: DispatchSourceTimer?
    private var lastReading: ContinuousClock.Instant?
    private var lastStreamData: ContinuousClock.Instant?
    private var lastExportData: ContinuousClock.Instant?

    func start(key: String) {
        stop()
        guard let bytes = Self.parseKey(key) else {
            presentation.status = "Enter the 64 hex digits from the tracker"
            return
        }
        pairingKey = bytes
        presentation.interruptions = 0
        presentation.active = true
        connect()
    }

    private func connect() {
        guard foreground, presentation.active, pairingKey != nil else { return }
        presentation.status = pairedTracker == nil ? "Looking for tracker" : "Reconnecting to tracker"
        if let manager {
            beginDiscovery(manager)
        } else {
            manager = CBCentralManager(delegate: self, queue: queue)
        }
        let token = generation
        queue.asyncAfter(deadline: .now() + (pairedTracker == nil ? 45 : 15)) { [weak self] in
            guard let self, self.generation == token, self.lastStreamData == nil else { return }
            self.recover("Connection timed out")
        }
    }

    private func beginDiscovery(_ central: CBCentralManager) {
        guard foreground, presentation.active, peripheral == nil, central.state == .poweredOn else { return }
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
        presentation.status = "Connecting to tracker"
        central.stopScan()
        peer.delegate = self
        central.connect(peer)
    }

    func stop(_ message: String = "Disconnected", keepExport: Bool = false) {
        let accepted = keepExport ? capture : nil
        let exporting = presentation.exporting
        presentation.active = false
        pairingKey = nil
        pairedTracker = nil
        requiresExport = false
        retries = 0
        retireConnection()
        if let accepted {
            presentation.history = accepted
            presentation.exporting = exporting
        }
        presentation.status = message
    }

    func setForeground(_ value: Bool) {
        guard foreground != value else { return }
        foreground = value
        guard presentation.active else { return }
        if value {
            retries = 0
            connect()
        } else {
            retireConnection()
            presentation.status = "Paused"
        }
    }

    private func recover(_ message: String) {
        guard pairedTracker != nil, retries < 3 else {
            stop(message)
            return
        }
        presentation.interruptions += 1
        retireConnection()
        presentation.status = "Reconnecting to tracker"
        let delay = 1 << retries
        retries += 1
        let token = generation
        queue.asyncAfter(deadline: .now() + .seconds(delay)) { [weak self] in
            guard let self, self.generation == token else { return }
            self.connect()
        }
    }

    private func retireConnection() {
        generation = UUID()
        presentation.reading = nil
        lastReading = nil
        lastStreamData = nil
        lastExportData = nil
        relayTimer?.cancel()
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
        tileTask?.cancel()
        tileTask = nil
        wantedTile = nil
        offeredTile = nil
        ingressPayload = 19
        writesSinceRead = 0
        writeRetries = 0
        writeDeadline = nil
        pending = nil
        operation = nil
        relaySending = false
        notifying = false
        requestedExport = false
        notificationStream = TrackerNotificationStream()
        acknowledgementPending = false
        relayBytes = 0
        frames = TrackerExportFrames()
        capture = TrackerHistory()
        presentation.history = TrackerHistory()
        lastPlot = nil
        presentation.exporting = false
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
                guard let self, self.generation == token, self.relay == nil else {
                    conn.cancel()
                    return
                }
                self.relay = conn
                conn.start(queue: self.queue)
                self.receiveCiphertext(conn, token: token)
            }
            listener.stateUpdateHandler = { [weak self, weak listener] state in
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
            listener.start(queue: queue)
        } catch {
            stop("Could not start secure connection")
        }
    }

    private func startRelayTimer(token: UUID) {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(15), leeway: .milliseconds(2))
        timer.setEventHandler { [weak self] in
            guard let self, self.generation == token else { return }
            let now = ContinuousClock.now
            if let deadline = self.writeDeadline, now >= deadline {
                // The write may have reached the tracker. Reconnect instead
                // of replaying ciphertext whose delivery is unknown.
                self.recover("Bluetooth write timed out")
                return
            }
            if !self.refreshTelemetry(at: now) {
                if self.requestedExport {
                    self.stop("Sensor export incomplete. Sensor samples stopped.", keepExport: true)
                } else {
                    self.recover("Tracker data stopped")
                }
                return
            }
            self.pump()
        }
        relayTimer = timer
        timer.activate()
    }

    /// Returns false after the authenticated stream stops delivering data.
    func refreshTelemetry(at now: ContinuousClock.Instant) -> Bool {
        if let last = lastExportData ?? lastStreamData, last.duration(to: now) > .seconds(12) {
            return false
        }
        if let last = lastReading, last.duration(to: now) > .seconds(4), presentation.reading != nil {
            presentation.reading = nil
            if !notifying { presentation.status = "Waiting for tracker data" }
        }
        return true
    }

    private func connectTLS(port: NWEndpoint.Port, options: NWProtocolTLS.Options, token: UUID) {
        presentation.status = "Authenticating tracker"
        let client = NWConnection(host: Self.loopback, port: port, using: NWParameters(tls: options))
        tlsClient = client
        client.stateUpdateHandler = { [weak self, weak client] state in
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
                self.presentation.status = self.notifying ? "Secure connection" : "Dashboard only"
                if self.notifying {
                    self.presentation.exporting = true
                }
                self.receiveTelemetry(client, token: token)
            case .failed: self.recover("Tracker authentication or connection failed")
            default: break
            }
        }
        client.start(queue: queue)
    }

    private func receiveTelemetry(_ client: NWConnection, token: UUID) {
        client.receive(minimumIncompleteLength: 1, maximumLength: 1024) { [weak self, weak client] data, _, done, error in
            guard let self, let client, self.generation == token else { return }
            if let data, !self.receiveAuthenticatedData(data, at: .now) { return }
            if done || error != nil { self.recover("Secure connection closed"); return }
            self.receiveTelemetry(client, token: token)
        }
    }

    /// Applies the plaintext read from the authenticated tracker connection.
    func receiveAuthenticatedData(_ data: Data, at now: ContinuousClock.Instant) -> Bool {
        var newest: TrackerTelemetry?
        var measurements: [TrackerMeasurement] = []
        let terminal: String?
        do {
            try frames.receive(data, snapshot: { newest = $0 }, measurement: { measurements.append($0) })
            terminal = nil
        } catch TrackerFrames.FrameError.captureOverflow {
            terminal = "Sensor export incomplete. Tracker buffer filled."
        } catch let TrackerFrames.FrameError.captureOverflowAt(source, sequence) {
            let name = ["Onboard IMU", "Module IMU", "GNSS"][Int(source) - 1]
            terminal = "Sensor export incomplete. First missing: \(name) #\(sequence)."
        } catch {
            terminal = "Invalid tracker telemetry"
        }
        if !measurements.isEmpty || newest != nil {
            lastStreamData = now
            retries = 0
        }
        if !measurements.isEmpty {
            lastExportData = now
            for item in measurements { capture.append(item) }
            capture.trim()
            if lastPlot.map({ $0.duration(to: now) >= .milliseconds(100) }) ?? true {
                lastPlot = now
                presentation.history = capture
            }
        }
        if let terminal {
            stop(terminal, keepExport: true)
            return false
        }
        if let newest {
            presentation.reading = newest
            lastReading = now
            requestExport(telemetry: newest, at: now)
            if let tlsClient { updateTile(newest.tile, client: tlsClient, token: generation) }
        }
        if capture.hasGaps {
            presentation.status = "Sensor export incomplete. Samples missing."
        } else if newest != nil {
            presentation.status = notifying ? "Secure connection" : "Dashboard only"
        }
        return true
    }

    private func requestExport(telemetry: TrackerTelemetry, at now: ContinuousClock.Instant) {
        guard notifying, !requestedExport, let tlsClient else { return }
        requestedExport = true
        lastExportData = now
        let request: Data
        if telemetry.imuScaleBits?.count == 2 {
            request = TrackerExportFrames.timedRequest
        } else {
            request = telemetry.exportVersion == 2 ? TrackerExportFrames.compactRequest : TrackerExportFrames.request
        }
        let token = generation
        tlsClient.send(content: request, completion: .contentProcessed { [weak self] error in
            guard let self, self.generation == token, error != nil else { return }
            self.recover("Could not request sensor export")
        })
    }

    private func updateTile(_ wanted: TrackerTile?, client: NWConnection, token: UUID) {
        wantedTile = wanted
        if wanted == nil { offeredTile = nil }
        guard let wanted, wanted != offeredTile, tileTask == nil else { return }
        offeredTile = wanted
        tileTask = Task { [weak self, weak client] in
            let reply = await wanted.download()
            guard let self, let client, !Task.isCancelled else { return }
            self.queue.async {
                guard self.generation == token else { return }
                if self.wantedTile == wanted {
                    self.sendTile(reply, from: 0, client: client, token: token)
                } else {
                    self.tileTask = nil
                    self.updateTile(self.wantedTile, client: client, token: token)
                }
            }
        }
    }

    // Keep TLS writes smaller than the tracker record buffer. Once started,
    // finish the response even if the viewport changes so framing stays intact.
    private func sendTile(_ reply: Data, from offset: Int, client: NWConnection, token: UUID) {
        if offset == reply.count {
            tileTask = nil
            updateTile(wantedTile, client: client, token: token)
            return
        }
        let end = min(offset + 512, reply.count)
        client.send(content: reply.subdata(in: offset..<end), completion: .contentProcessed { [weak self, weak client] error in
            guard let self, let client, self.generation == token else { return }
            if error != nil { self.recover("Secure connection closed"); return }
            self.sendTile(reply, from: end, client: client, token: token)
        })
    }

    private func receiveCiphertext(_ conn: NWConnection, token: UUID) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: notifying ? ingressPayload : 20) { [weak self] data, _, done, error in
            guard let self, self.generation == token else { return }
            guard !done, error == nil, let data, !data.isEmpty else {
                self.recover("Secure connection closed")
                return
            }
            self.pending = data
            self.pump()
        }
    }

    private func pump() {
        guard let peripheral, let ingress, let egress, relay != nil else { return }
        if notifying, acknowledgementPending, peripheral.canSendWriteWithoutResponse {
            acknowledgementPending = false
            peripheral.writeValue(notificationStream.acknowledgement, for: ingress, type: .withoutResponse)
        }
        guard operation == nil, !relaySending else { return }
        if let pending, notifying || writesSinceRead < 4 {
            if !notifying { writesSinceRead += 1 }
            operation = .write
            if writeDeadline == nil { writeDeadline = .now + .seconds(2) }
            peripheral.writeValue(notifying ? Data([0]) + pending : pending, for: ingress, type: .withResponse)
        } else if !notifying {
            writesSinceRead = 0
            operation = .read
            peripheral.readValue(for: egress)
        }
    }
}

extension TrackerConnection: CBCentralManagerDelegate, CBPeripheralDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard central === manager else { return }
        switch central.state {
        case .poweredOn: beginDiscovery(central)
        case .unknown, .resetting: break
        case .poweredOff:
            disconnectingPeer = nil
            if presentation.active { recover("Bluetooth is unavailable") }
        default: stop("Bluetooth is unavailable")
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard central === manager, foreground, presentation.active, self.peripheral == nil, disconnectingPeer == nil,
              pairedTracker == nil || peripheral.identifier == pairedTracker else { return }
        if (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID])?.contains(Self.exportService.uuid) == true {
            requiresExport = true
        }
        connect(central, to: peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard central === manager, peripheral === self.peripheral else { return }
        presentation.status = "Reading tracker services"
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
        ingress = service.characteristics?.first {
            $0.uuid == profile.ingress && $0.properties.contains(.write)
                && (!profile.notifications || $0.properties.contains(.writeWithoutResponse))
        }
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
            guard !notifying else { return }
            notifying = true
            // CoreBluetooth may cache the service layout across firmware updates.
            // Test the direct ATT size using the existing write characteristic.
            let limit = min(244, peer.maximumWriteValueLength(for: .withoutResponse),
                            peer.maximumWriteValueLength(for: .withResponse))
            if let ingress, limit > 20 {
                var probe = Data(repeating: 0, count: limit)
                probe[0] = 2
                operation = .probe(payload: limit - 1)
                peer.writeValue(probe, for: ingress, type: .withResponse)
            } else {
                establishRelay()
            }
        } else if notifying && (error != nil || !characteristic.isNotifying) {
            recover("Tracker notifications stopped")
        }
    }

    func peripheral(_ peer: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard peer === peripheral, characteristic === ingress else { return }
        if case .probe(let payload) = operation {
            operation = nil
            if error == nil { ingressPayload = payload }
            establishRelay()
            return
        }
        guard operation == .write else { return }
        operation = nil
        // The tracker rejects a GATT write before enqueuing any bytes when
        // its bounded ingress is busy. Retry the identical ciphertext.
        if (error as? CBATTError)?.code == .unlikelyError, writeRetries < 32 {
            writeRetries += 1
            writesSinceRead = 4
            return
        }
        guard error == nil, let relay else { recover("Bluetooth write failed"); return }
        writeRetries = 0
        writeDeadline = nil
        pending = nil
        receiveCiphertext(relay, token: generation)
        // The periodic read gives the tracker a chance to drain outgoing ciphertext.
    }

    func peripheralIsReady(toSendWriteWithoutResponse peer: CBPeripheral) {
        guard peer === peripheral else { return }
        pump()
    }

    func peripheral(_ peer: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard peer === peripheral, characteristic === egress, notifying || operation == .read else { return }
        guard error == nil, let packet = characteristic.value,
              packet.count <= (notifying ? 244 : 20), let relay else {
            recover("Bluetooth data failed")
            return
        }
        let data: Data
        if notifying {
            guard packet.count > 4 else { recover("Invalid Bluetooth notification"); return }
            // Keep this offset unacknowledged if local relay storage is full.
            guard relayBytes + packet.count - 4 <= 8192 else { return }
            acknowledgementPending = true
            guard let accepted = notificationStream.accept(packet) else { return }
            data = accepted
        } else {
            operation = nil
            guard !packet.isEmpty else { return }
            data = packet
        }
        relayBytes += data.count
        relaySending = !notifying
        let token = generation
        relay.send(content: data, completion: .contentProcessed { [weak self] error in
            guard let self, self.generation == token else { return }
            self.relayBytes -= data.count
            self.relaySending = false
            if error != nil { self.recover("Secure connection closed") }
        })
    }
}
