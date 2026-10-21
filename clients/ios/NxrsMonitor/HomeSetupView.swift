import SwiftUI
import UniformTypeIdentifiers
import NxrsMonitorCore

struct HomeSetupView: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject private var library: HomeLibrary
    let replacing: Bool
    @Environment(\.dismiss) private var dismiss
    @State private var manual = false
    @State private var importing = false
    @State private var gateway = ""
    @State private var media = ""
    @State private var token = ""
    @State private var homeName = "My home"
    @State private var importError: String?
    @State private var advanced = false
    @State private var showingLibrary = false
    @FocusState private var focused: Field?
    private enum Field: Hashable { case name, gateway, key, media }

    init(model: MonitorModel, replacing: Bool) {
        self.model = model
        self.library = model.library
        self.replacing = replacing
    }

    var body: some View {
        NavigationStack {
            Group {
                if manual { enrollmentForm }
                else { welcome }
            }
            .background(HomeTheme.canvas)
            .toolbar {
                if replacing {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { model.cancelPairing(); token = ""; dismiss() }
                    }
                } else if manual {
                    ToolbarItem(placement: .cancellationAction) { Button("Back") { manual = false; focused = nil } }
                }
            }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.json], allowsMultipleSelection: false) { result in
                do {
                    guard let url = try result.get().first else { return }
                    let scoped = url.startAccessingSecurityScopedResource()
                    defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                    let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                    guard size <= 16_384 else { throw MonitorError.responseTooLarge }
                    applyInvitation(try Data(contentsOf: url))
                } catch { importError = "This invitation could not be opened. Choose a valid Nxrs Home invitation JSON file." }
            }
            .onChange(of: model.enrollmentID) { _ in token = ""; if replacing { dismiss() } }
            .onDisappear { token = ""; model.cancelPairing() }
            .sheet(isPresented: $showingLibrary) {
                HomeLibraryView(library: library, dismissible: true)
            }
        }.tint(HomeTheme.forest)
    }

    private var welcome: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                HomeWordmark().padding(.top, 12)
                ZStack(alignment: .topTrailing) {
                    HomeRoomIllustration()
                    HomeMark(light: true).frame(width: 76, height: 76).rotationEffect(.degrees(9))
                        .padding(22)
                }.frame(height: 260).clipShape(RoundedRectangle(cornerRadius: 34, style: .continuous))
                VStack(alignment: .leading, spacing: 12) {
                    Text("A little closer\nto home.")
                        .font(.system(.largeTitle, design: .rounded).weight(.bold))
                        .foregroundStyle(HomeTheme.ink).fixedSize(horizontal: false, vertical: true)
                    Text("See the familiar. Save the moments. Keep your home in view, wherever you are.")
                        .font(.body).foregroundStyle(HomeTheme.secondary).fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 18) {
                    setupBenefit("Live, at a glance", detail: "Your cameras in one calm, simple space.", symbol: "video")
                    setupBenefit("Moments that stay yours", detail: "Photos and clips saved only on this iPhone.", symbol: "lock.shield")
                    setupBenefit("Your home, your connection", detail: "Connect directly to a home you trust.", symbol: "house")
                }
                VStack(spacing: 14) {
                    Button { manual = true } label: { HStack { Text(replacing ? "Connect another home" : "Connect your home"); Image(systemName: "arrow.right") } }
                        .buttonStyle(PrimaryHomeButton()).accessibilityIdentifier("connect-home")
                    Button { importing = true } label: { Label("I have an invitation", systemImage: "square.and.arrow.down") }
                        .font(.subheadline.weight(.semibold)).frame(minHeight: 44).foregroundStyle(HomeTheme.forest)
                    if !library.captures.isEmpty {
                        Button { showingLibrary = true } label: {
                            Label("View saved moments", systemImage: "square.stack")
                        }.font(.subheadline).frame(minHeight: 44).foregroundStyle(HomeTheme.forest)
                    }
                }
                if let importError { Text(importError).font(.caption).foregroundStyle(HomeTheme.warning) }
                Text("No account to create. Use the home address and viewer key supplied by your home owner.")
                    .font(.caption).foregroundStyle(HomeTheme.secondary).multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
            }.padding(.horizontal, 26).padding(.bottom, 30)
        }.navigationBarTitleDisplayMode(.inline)
    }

    private var enrollmentForm: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HomeMark().frame(width: 60, height: 60)
                VStack(alignment: .leading, spacing: 10) {
                    Text("Make yourself\nat home.")
                        .font(.system(.largeTitle, design: .rounded).weight(.bold)).foregroundStyle(HomeTheme.ink)
                    Text("Connect the home you control, or one shared with you.")
                        .font(.subheadline).foregroundStyle(HomeTheme.secondary)
                }
                VStack(alignment: .leading, spacing: 18) {
                    inputField("HOME NAME", symbol: "house") {
                        TextField("My home", text: $homeName).focused($focused, equals: .name)
                            .textContentType(.nickname).submitLabel(.next).onSubmit { focused = .gateway }
                    }
                    inputField("HOME ADDRESS", symbol: "link") {
                        TextField("https://home.example.com", text: $gateway).focused($focused, equals: .gateway)
                            .keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never)
                            .autocorrectionDisabled().submitLabel(.next).onSubmit { focused = .key }
                            .accessibilityIdentifier("gateway-address")
                    }
                    inputField("VIEWER KEY", symbol: "key.horizontal") {
                        SecureField("Your private viewer key", text: $token).focused($focused, equals: .key)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().submitLabel(.go)
                            .accessibilityIdentifier("viewer-key")
                    }
                }
                HStack(spacing: 18) {
                    Button { importing = true } label: { Label("Import invitation", systemImage: "doc.badge.plus") }
                    Spacer()
                    PasteButton(payloadType: String.self) { values in
                        guard let value = values.first else { return }
                        applyInvitation(Data(value.utf8))
                    }.labelStyle(.titleOnly).buttonBorderShape(.capsule)
                        .accessibilityLabel("Paste home invitation")
                }.font(.caption.weight(.semibold)).foregroundStyle(HomeTheme.forest)
                DisclosureGroup("Advanced connection", isExpanded: $advanced) {
                    VStack(alignment: .leading, spacing: 8) {
                        TextField("Separate HTTPS video origin (optional)", text: $media)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                            .padding(.vertical, 10).focused($focused, equals: .media)
                        Text("Leave this empty unless your invitation uses a different address for video.")
                            .font(.caption).foregroundStyle(HomeTheme.secondary)
                    }.padding(.top, 8)
                }.font(.subheadline).foregroundStyle(HomeTheme.secondary)
                if let error = importError ?? model.pairingError {
                    Label(error, systemImage: "exclamationmark.circle")
                        .font(.subheadline).foregroundStyle(HomeTheme.warning).fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    focused = nil
                    model.enroll(gateway: gateway, media: media, token: token, homeName: homeName)
                } label: {
                    HStack(spacing: 10) {
                        if model.pairing { ProgressView().tint(HomeTheme.darkForest) }
                        Text(model.pairing ? "Connecting to your home…" : "Connect securely")
                        if !model.pairing { Image(systemName: "arrow.right") }
                    }
                }.buttonStyle(PrimaryHomeButton()).disabled(gateway.isEmpty || token.isEmpty || model.pairing)
                    .opacity(gateway.isEmpty || token.isEmpty ? 0.5 : 1)
                    .accessibilityIdentifier("save-home")
                VStack(alignment: .leading, spacing: 10) {
                    Label("A connection you choose", systemImage: "lock.shield").font(.subheadline.weight(.semibold))
                    Text("We verify your home's HTTPS certificate. Your viewer key is kept in this iPhone's Keychain. Away from home, connect your private-network app first.")
                        .font(.caption).fixedSize(horizontal: false, vertical: true)
                    if replacing { Text("Your current home stays connected until the new one is verified.").font(.caption) }
                }.foregroundStyle(HomeTheme.secondary)
            }.padding(26)
        }.scrollDismissesKeyboard(.interactively).navigationBarTitleDisplayMode(.inline)
    }

    private func setupBenefit(_ title: String, detail: String, symbol: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol).font(.title3).frame(width: 28).foregroundStyle(HomeTheme.forest)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(HomeTheme.ink)
                Text(detail).font(.caption).foregroundStyle(HomeTheme.secondary)
            }
        }
    }

    private func inputField<Content: View>(_ title: String, symbol: String,
                                            @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.caption2.weight(.semibold)).tracking(1.5).foregroundStyle(HomeTheme.secondary)
            HStack(spacing: 12) { Image(systemName: symbol).foregroundStyle(HomeTheme.secondary); content() }
                .font(.body).padding(17).background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 17))
                .overlay(RoundedRectangle(cornerRadius: 17).strokeBorder(HomeTheme.line, lineWidth: 1))
        }
    }

    private func applyInvitation(_ data: Data) {
        do {
            let invitation = try EnrollmentInvitation(data: data)
            gateway = invitation.gateway.absoluteString
            media = invitation.mediaOrigin?.absoluteString ?? ""
            token = invitation.withCredential { $0 }
            homeName = invitation.homeName
            importError = nil; manual = true
        } catch { importError = "This invitation is not valid. Use a version 1 invitation with a trusted HTTPS address and viewer key." }
    }
}
