import SwiftUI
import NxrsMonitorCore
import NxrsMonitorNative

struct MonitorView: View {
    @ObservedObject var model: MonitorModel
    @State private var gateway = ""
    @State private var media = ""
    @State private var token = ""
    @State private var fullscreen = false

    var body: some View {
        NavigationStack {
            Group {
                if model.enrolled { monitoring }
                else { enrollment }
            }
            .navigationTitle("Nxrs Monitor")
            .toolbar {
                if model.enrolled {
                    ToolbarItem(placement: .navigationBarTrailing) {
                        Menu {
                            Button("Refresh cameras") { model.reload() }
                            Button("Forget gateway", role: .destructive) { model.forget() }
                        } label: { Image(systemName: "gearshape") }
                    }
                }
            }
            .fullScreenCover(isPresented: $fullscreen) {
                NavigationStack {
                    player.padding()
                        .toolbar { ToolbarItem(placement: .navigationBarTrailing) {
                            Button("Done") { fullscreen = false }
                        } }
                }
            }
        }
    }

    private var enrollment: some View {
        Form {
            Section("Trusted gateway") {
                TextField("HTTPS gateway URL", text: $gateway)
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("HTTPS media origin (when different)", text: $media)
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                SecureField("Viewer credential", text: $token)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                Text("Only enroll a gateway you control or trust. HTTPS certificates must be trusted by iOS. Use the separate WireGuard app for remote network access.")
                    .font(.footnote).foregroundStyle(.secondary)
                Button("Save and connect") {
                    model.enroll(gateway: gateway, media: media, token: token)
                    token = ""
                }.disabled(gateway.isEmpty || token.isEmpty)
            }
            if let error = model.errorMessage { Text(error).foregroundStyle(.red) }
        }
    }

    private var monitoring: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                player
                if let error = model.errorMessage { Text(error).font(.callout).foregroundStyle(.red) }
                HStack {
                    Button("Reconnect") { if let camera = model.selected { model.select(camera) } }
                        .disabled(model.selected == nil)
                    Button("Stop") { model.stop() }
                    Spacer()
                    Button { fullscreen = true } label: { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                        .accessibilityLabel("Full screen")
                }
                Text("Cameras").font(.headline)
                if model.busy { ProgressView("Loading cameras") }
                if model.cameras.isEmpty && !model.busy {
                    Text("No cameras are available. Check your gateway access, then refresh.").foregroundStyle(.secondary)
                }
                ForEach(model.cameras, id: \.cameraID) { camera in
                    Button { model.select(camera) } label: {
                        HStack {
                            Image(systemName: "video")
                            VStack(alignment: .leading) {
                                Text(camera.name).font(.headline)
                                Text(camera.source == .synthetic ? "Synthetic test source" : "Physical camera")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if model.selected?.cameraID == camera.cameraID { Image(systemName: "checkmark") }
                        }.padding().frame(maxWidth: .infinity, alignment: .leading)
                    }.buttonStyle(.bordered).disabled(!camera.liveVideo)
                }
                Text("Live means incoming decoded frames. Capture-to-display freshness and source health are not yet measured.")
                    .font(.footnote).foregroundStyle(.secondary)
            }.padding()
        }
    }

    private var player: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.selected?.name ?? "Select a camera").font(.title3).bold()
            ZStack {
                RoundedRectangle(cornerRadius: 12).fill(.black)
                if let peer = model.peer {
                    VideoSurface(peer: peer).id(model.videoID).clipShape(RoundedRectangle(cornerRadius: 12))
                }
                if !model.live {
                    Text(model.status).foregroundStyle(.white).padding()
                        .background(.black.opacity(0.75), in: RoundedRectangle(cornerRadius: 8))
                }
                if model.selected?.source == .synthetic {
                    VStack { HStack { Text("SYNTHETIC TEST SOURCE").font(.caption).bold()
                        .padding(6).background(.orange, in: Capsule()); Spacer() }; Spacer() }.padding(8)
                }
            }.aspectRatio(4.0 / 3.0, contentMode: .fit)
            Label(model.status, systemImage: model.live ? "checkmark.circle.fill" : "clock")
                .font(.subheadline).accessibilityIdentifier("playback-status")
            Label(model.detectionStatus, systemImage: model.personDetected ? "person.fill.viewfinder" : "person.crop.rectangle")
                .foregroundStyle(model.personDetected ? Color.orange : Color.secondary)
                .accessibilityIdentifier("detection-status")
            if model.detectionHistoryIncomplete {
                Text("Detection history interrupted").font(.caption).foregroundStyle(.secondary)
                    .accessibilityIdentifier("detection-history-gap")
            }
        }
    }
}
