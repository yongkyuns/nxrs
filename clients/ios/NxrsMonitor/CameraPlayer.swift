import SwiftUI
import NxrsMonitorNative

struct CameraDetectionStatus: View {
    @ObservedObject var model: MonitorModel
    var dark = false
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(model.detectionStatus, systemImage: model.personDetected ? "person.fill.viewfinder" : "person.crop.rectangle")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(model.personDetected ? Color.orange : dark ? Color.white.opacity(0.7) : HomeTheme.secondary)
                .accessibilityIdentifier("detection-status")
            if model.detectionHistoryIncomplete {
                Text("Detection history interrupted").font(.caption)
                    .foregroundStyle(dark ? Color.white.opacity(0.7) : HomeTheme.secondary)
                    .accessibilityIdentifier("detection-history-gap")
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct PlaybackBadge: View {
    let status: String
    let live: Bool
    var body: some View {
        Label(status, systemImage: live ? "dot.radiowaves.left.and.right" : status == "Paused" ? "pause.fill" : "circle.dotted")
            .font(.caption.weight(.semibold)).lineLimit(2)
            .padding(.horizontal, 11).padding(.vertical, 8)
            .foregroundStyle(live ? HomeTheme.darkForest : .white)
            .background(live ? HomeTheme.lime : Color.black.opacity(0.55), in: Capsule())
            .accessibilityIdentifier("playback-status")
    }
}

struct CameraLiveCanvas: View {
    @ObservedObject var model: MonitorModel
    let interactive: Bool
    @State private var zoom: CGFloat = 1
    @State private var previousZoom: CGFloat = 1
    @State private var offset = CGSize.zero
    @State private var previousOffset = CGSize.zero

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color.black
                if let peer = model.peer {
                    VideoSurface(peer: peer).id(model.videoID)
                        .scaleEffect(zoom).offset(offset)
                        .accessibilityLabel(model.live ? "Live camera video" : "Camera video is not current")
                        .accessibilityHint(interactive ? "Use the zoom control or pinch to magnify" : "Expand for a larger view")
                        .accessibilityHidden(!model.live)
                } else if model.isPreview {
                    HomeRoomIllustration().overlay(alignment: .bottomTrailing) {
                        Text("ILLUSTRATION").font(.caption2.weight(.semibold)).tracking(1.5)
                            .foregroundStyle(HomeTheme.darkForest.opacity(0.7)).padding(14)
                    }
                } else {
                    HomeTheme.darkForest
                    Image(systemName: "video").font(.system(size: 62, weight: .ultraLight)).foregroundStyle(.white.opacity(0.12))
                }
                if !model.live && !model.isPreview {
                    VStack(spacing: 14) {
                        if model.peer != nil { ProgressView().tint(.white) }
                        else { Image(systemName: model.status == "Paused" ? "pause.circle" : "video.slash")
                            .font(.system(size: 32, weight: .light)) }
                        Text(model.status == "Paused" ? "Viewing paused" : model.status)
                            .font(.headline).multilineTextAlignment(.center)
                        if model.status == "Paused" || model.status == "Offline" || model.status == "Connection failed" {
                            Button { model.reconnect() } label: {
                                Label(model.status == "Paused" ? "Watch live" : "Reconnect", systemImage: "play.fill")
                                    .font(.subheadline.weight(.semibold)).padding(.horizontal, 20).frame(minHeight: 46)
                                    .background(HomeTheme.lime, in: Capsule()).foregroundStyle(HomeTheme.darkForest)
                            }.buttonStyle(.plain).accessibilityIdentifier("resume-camera")
                        }
                    }.foregroundStyle(.white).padding(24)
                        .background(.black.opacity(model.peer == nil ? 0 : 0.70), in: RoundedRectangle(cornerRadius: 20))
                }
                if model.selected?.source == .synthetic {
                    VStack { Spacer(); HStack {
                        Label("Synthetic test source", systemImage: "testtube.2").font(.caption.weight(.semibold))
                            .padding(8).background(.orange, in: Capsule()).foregroundStyle(.black)
                        Spacer()
                    } }.padding(12)
                }
                if interactive, model.live {
                    VStack { Spacer(); HStack {
                        Spacer()
                        Button {
                            zoom = zoom > 1 ? 1 : 2
                            previousZoom = zoom; offset = .zero; previousOffset = .zero
                        } label: {
                            Text(zoom, format: .number.precision(.fractionLength(0...1))) + Text("×")
                        }.font(.caption.weight(.semibold)).frame(width: 48, height: 44)
                            .background(.black.opacity(0.6), in: Capsule()).foregroundStyle(.white)
                            .accessibilityLabel("Digital zoom")
                            .accessibilityValue("\(Double(zoom).formatted(.number.precision(.fractionLength(0...1)))) times")
                            .accessibilityHint("Double-tap to toggle one and two times zoom.")
                            .accessibilityAdjustableAction { direction in
                                switch direction {
                                case .increment: zoom = min(4, zoom + 0.5)
                                case .decrement: zoom = max(1, zoom - 0.5)
                                @unknown default: return
                                }
                                previousZoom = zoom; offset = .zero; previousOffset = .zero
                            }
                    } }.padding(14)
                }
            }
            .clipped()
            .gesture(MagnificationGesture().onChanged { value in
                guard interactive else { return }
                zoom = min(4, max(1, previousZoom * value))
                offset = bounded(offset, in: geometry.size)
            }.onEnded { _ in previousZoom = zoom; previousOffset = offset }, including: interactive ? .all : .none)
            .simultaneousGesture(DragGesture(minimumDistance: 10).onChanged { value in
                guard interactive, zoom > 1 else { return }
                offset = bounded(CGSize(width: previousOffset.width + value.translation.width,
                                        height: previousOffset.height + value.translation.height), in: geometry.size)
            }.onEnded { _ in previousOffset = offset }, including: interactive ? .all : .none)
            .onChange(of: model.videoID) { _ in zoom = 1; previousZoom = 1; offset = .zero; previousOffset = .zero }
        }
    }

    private func bounded(_ proposed: CGSize, in size: CGSize) -> CGSize {
        CGSize(width: min(size.width * (zoom - 1) / 2, max(-size.width * (zoom - 1) / 2, proposed.width)),
               height: min(size.height * (zoom - 1) / 2, max(-size.height * (zoom - 1) / 2, proposed.height)))
    }
}

struct CameraCaptureControls: View {
    @ObservedObject var model: MonitorModel
    var dark = false
    @Environment(\.dynamicTypeSize) private var typeSize
    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                captureButton("Snapshot", symbol: "camera", highlighted: false,
                              enabled: model.live && !model.snapshotBusy, action: model.takeSnapshot)
                captureButton(model.isRecording ? "Stop clip" : model.finalizingClip ? "Saving…" : "Record clip",
                              symbol: model.isRecording ? "stop.fill" : "record.circle",
                              highlighted: model.isRecording,
                              enabled: model.isRecording || model.canRecord, action: model.toggleRecording)
                captureButton(model.peer == nil ? "Watch" : "Pause", symbol: model.peer == nil ? "play" : "pause",
                              highlighted: false, enabled: model.selected != nil,
                              action: { if model.peer == nil { model.reconnect() } else { model.stop() } })
            }
            if let since = model.recordingSince {
                HStack(spacing: 6) {
                    Image(systemName: "record.circle.fill").foregroundStyle(.red)
                    Text("Recording").fontWeight(.medium)
                    Text(since, style: .timer).monospacedDigit()
                    Spacer()
                    Text("60 sec max")
                }.font(.caption).foregroundStyle(dark ? .white.opacity(0.7) : HomeTheme.secondary)
            }
        }
    }

    private func captureButton(_ title: String, symbol: String, highlighted: Bool,
                               enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 9) {
                Image(systemName: symbol).font(.title3.weight(.medium))
                Text(title).font(.caption.weight(.semibold)).multilineTextAlignment(.center)
            }.frame(maxWidth: .infinity).frame(minHeight: typeSize.isAccessibilitySize ? 112 : 84)
                .padding(.horizontal, 3)
                .foregroundStyle(highlighted ? Color.red : dark ? Color.white : HomeTheme.ink)
                .background(highlighted ? Color.red.opacity(0.10) : dark ? Color.white.opacity(0.1) : HomeTheme.surface,
                            in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                .opacity(enabled ? 1 : 0.45)
        }.buttonStyle(.plain).disabled(!enabled).accessibilityLabel(title)
    }
}

struct FullScreenCameraView: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject var settings: HomeSettings
    @Environment(\.dismiss) private var dismiss
    @State private var showInfo = false
    var body: some View {
        GeometryReader { geometry in
            let landscape = geometry.size.width > geometry.size.height
            VStack(spacing: landscape ? 8 : 24) {
                HStack {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.headline).frame(width: 46, height: 46)
                            .background(.white.opacity(0.1), in: Circle())
                    }.buttonStyle(.plain).accessibilityLabel("Close full screen")
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.selected.map { settings.preferences.displayName(for: $0) } ?? "Your camera")
                            .font(.headline)
                        Text(model.isPreview ? "Design preview" : model.status).font(.caption).foregroundStyle(.white.opacity(0.65))
                    }
                    Spacer()
                    Button { showInfo = true } label: {
                        Image(systemName: "info.circle").font(.title3).frame(width: 46, height: 46)
                    }.buttonStyle(.plain).accessibilityLabel("Connection details")
                }.foregroundStyle(.white).padding(.horizontal, 20)
                CameraDetectionStatus(model: model, dark: true).padding(.horizontal, 22)
                Spacer(minLength: 0)
                if landscape {
                    HStack(spacing: 20) {
                        CameraLiveCanvas(model: model, interactive: true)
                            .aspectRatio(4.0 / 3.0, contentMode: .fit)
                        ScrollView {
                            VStack(spacing: 12) {
                                Button { model.takeSnapshot() } label: { Label("Snapshot", systemImage: "camera") }
                                    .disabled(!model.live || model.snapshotBusy)
                                Button { model.toggleRecording() } label: {
                                    Label(model.isRecording ? "Stop clip" : "Record clip",
                                          systemImage: model.isRecording ? "stop.circle.fill" : "record.circle")
                                }.disabled(!model.isRecording && !model.canRecord)
                                Button { if model.live { model.stop() } else { model.reconnect() } } label: {
                                    Label(model.live ? "Pause" : "Watch", systemImage: model.live ? "pause" : "play")
                                }
                            }.buttonStyle(.bordered).controlSize(.large).tint(.white)
                        }.frame(maxWidth: 180)
                    }.padding(.horizontal, 22)
                } else {
                    CameraLiveCanvas(model: model, interactive: true)
                        .aspectRatio(4.0 / 3.0, contentMode: .fit).frame(maxHeight: .infinity)
                }
                Spacer(minLength: 0)
                if !landscape { CameraCaptureControls(model: model, dark: true).padding(.horizontal, 22) }
            }.padding(.vertical, landscape ? 4 : 12)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.black.ignoresSafeArea())
                .sheet(isPresented: $showInfo) { ConnectionDetailsView(model: model) }
        }.preferredColorScheme(.dark)
    }
}

struct ConnectionDetailsView: View {
    @ObservedObject var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List {
                Section("Live view") {
                    LabeledContent("Status", value: model.status)
                    LabeledContent("Person detection", value: model.detectionStatus)
                    if !model.resolution.isEmpty { LabeledContent("Resolution", value: model.resolution) }
                    if model.live { LabeledContent("Incoming frames", value: "\(model.framesPerSecond) / sec") }
                    LabeledContent("Camera", value: model.selected?.source == .synthetic ? "Synthetic test source" : "Physical camera")
                    LabeledContent("Audio", value: "Video only")
                }
                Section {
                    Text("Live means decoded video is arriving at this iPhone. It does not measure when the camera captured each image.")
                    Text("Pausing ends viewing on this iPhone. It does not turn off the camera or stop other viewers.")
                }.font(.subheadline).foregroundStyle(.secondary)
                Button("Reconnect live view") { model.reconnect(); dismiss() }
            }.navigationTitle("Connection details").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }
}
