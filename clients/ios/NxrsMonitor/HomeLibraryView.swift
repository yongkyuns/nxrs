import SwiftUI
import AVKit
import ImageIO
import NxrsMonitorCore

struct HomeLibraryView: View {
    @ObservedObject var library: HomeLibrary
    var dismissible = false
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var filter: Filter = .all
    @State private var search = ""
    @State private var selected: SavedCapture?
    @State private var deleting: SavedCapture?
    private enum Filter: String, CaseIterable, Identifiable {
        case all = "All", photo = "Photos", clip = "Clips", favorites = "Favorites"
        var id: String { rawValue }
    }
    private var filtered: [SavedCapture] {
        library.captures.filter { capture in
            (filter == .all || (filter == .photo && capture.kind == .photo)
             || (filter == .clip && capture.kind == .clip) || (filter == .favorites && capture.favorite))
                && (search.isEmpty || capture.cameraName.localizedCaseInsensitiveContains(search))
        }
    }
    private var days: [Date] {
        Array(Set(filtered.map { Calendar.current.startOfDay(for: $0.createdAt) })).sorted(by: >)
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("The moments you keep.").font(.title3.weight(.semibold)).foregroundStyle(HomeTheme.ink)
                        Text("Saved on this iPhone. Shared only by you.").font(.subheadline).foregroundStyle(HomeTheme.secondary)
                    }
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(Filter.allCases) { value in
                                Button { filter = value } label: {
                                    Text(value.rawValue).font(.caption.weight(.semibold)).padding(.horizontal, 19)
                                        .frame(minHeight: 44)
                                        .background(filter == value ? HomeTheme.lime : HomeTheme.surface, in: Capsule())
                                        .foregroundStyle(filter == value ? HomeTheme.darkForest : HomeTheme.secondary)
                                }.buttonStyle(.plain).accessibilityAddTraits(filter == value ? .isSelected : [])
                            }
                        }
                    }
                    if filtered.isEmpty {
                        HomeEmptyState(symbol: filter == .favorites ? "star" : "photo.stack",
                            title: library.captures.isEmpty ? "Keep a little moment" : "No matching moments",
                            message: library.captures.isEmpty
                                ? "Take a snapshot or record a short clip while watching a camera. You'll find it right here."
                                : "Try a different filter or camera name.")
                            .padding(.top, 38)
                    } else {
                        ForEach(days, id: \.self) { day in
                            VStack(alignment: .leading, spacing: 12) {
                                Text(dayTitle(day)).font(.headline).foregroundStyle(HomeTheme.ink)
                                LazyVGrid(columns: [GridItem(.adaptive(minimum: typeSize.isAccessibilitySize ? 260 : 145), spacing: 12)], spacing: 16) {
                                    ForEach(filtered.filter { Calendar.current.isDate($0.createdAt, inSameDayAs: day) }) { capture in
                                        CaptureCard(capture: capture, url: library.urls[capture.id]) { selected = capture }
                                            .contextMenu {
                                                Button(capture.favorite ? "Remove favorite" : "Favorite", systemImage: capture.favorite ? "star.slash" : "star") {
                                                    Task { await library.favorite(capture) }
                                                }
                                                if let url = library.urls[capture.id] { ShareLink(item: url) { Label("Share", systemImage: "square.and.arrow.up") } }
                                                Button("Delete", systemImage: "trash", role: .destructive) { deleting = capture }
                                            }
                                    }
                                }
                            }
                        }
                        HStack {
                            Image(systemName: "lock.shield")
                            Text("\(library.captures.count) moments · \(ByteCountFormatter.string(fromByteCount: library.bytes, countStyle: .file)) of 512 MB")
                        }.font(.caption).foregroundStyle(HomeTheme.secondary).padding(.top, 12)
                    }
                }.padding(22).padding(.bottom, 18)
            }.background(HomeTheme.canvas).navigationTitle("Library")
                .searchable(text: $search, prompt: "Find a camera's moments")
                .toolbar {
                    if dismissible { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                }
                .sheet(item: $selected) { capture in CaptureDetailView(capture: capture, library: library) }
                .confirmationDialog("Delete this moment?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                    if let capture = deleting { Button("Delete", role: .destructive) { Task { await library.remove(capture) }; deleting = nil } }
                } message: { Text("This removes the saved file from this iPhone. It cannot be undone.") }
                .alert("Library", isPresented: Binding(get: { library.errorMessage != nil }, set: { if !$0 { library.errorMessage = nil } })) {
                    Button("OK") { library.errorMessage = nil }
                } message: { Text(library.errorMessage ?? "") }
                .task { await library.reload() }
        }.tint(HomeTheme.forest)
    }

    private func dayTitle(_ date: Date) -> String {
        if Calendar.current.isDateInToday(date) { return "Today" }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday" }
        return date.formatted(.dateTime.month(.wide).day())
    }
}

private struct CaptureCard: View {
    let capture: SavedCapture
    let url: URL?
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 10) {
                ZStack(alignment: .bottomTrailing) {
                    CaptureThumbnail(url: url, kind: capture.kind).aspectRatio(4.0 / 3.0, contentMode: .fit)
                    if capture.kind == .clip {
                        Label(duration(capture.duration), systemImage: "play.fill").font(.caption2.weight(.semibold))
                            .padding(7).background(.black.opacity(0.6), in: Capsule()).foregroundStyle(.white).padding(8)
                    }
                    if capture.favorite {
                        VStack { HStack { Image(systemName: "star.fill").font(.caption).padding(8)
                            .background(.black.opacity(0.45), in: Circle()).foregroundStyle(HomeTheme.lime); Spacer() }; Spacer() }.padding(8)
                    }
                }.clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                VStack(alignment: .leading, spacing: 4) {
                    Text(capture.cameraName).font(.subheadline.weight(.semibold)).foregroundStyle(HomeTheme.ink)
                    Text(capture.createdAt, format: .dateTime.hour().minute()).font(.caption).foregroundStyle(HomeTheme.secondary)
                }
            }.contentShape(Rectangle())
        }.buttonStyle(.plain)
            .accessibilityLabel("\(capture.kind == .photo ? "Snapshot" : "Video clip") from \(capture.cameraName), \(capture.createdAt.formatted())")
    }
    private func duration(_ seconds: TimeInterval) -> String { "0:\(String(format: "%02d", Int(seconds.rounded(.up))))" }
}

private actor CaptureThumbnails {
    static let shared = CaptureThumbnails()
    private let images = NSCache<NSString, UIImage>()
    init() { images.countLimit = 32; images.totalCostLimit = 32 * 1024 * 1024 }
    func image(url: URL, kind: SavedCaptureKind, maximumSize: Int) async -> UIImage? {
        let key = (url.absoluteString + "@" + String(maximumSize)) as NSString
        if let cached = images.object(forKey: key) { return cached }
        let image: UIImage?
        if kind == .photo {
            if let source = CGImageSourceCreateWithURL(url as CFURL, nil),
               let cg = CGImageSourceCreateThumbnailAtIndex(source, 0,
                [kCGImageSourceCreateThumbnailFromImageAlways: true,
                 kCGImageSourceCreateThumbnailWithTransform: true,
                 kCGImageSourceThumbnailMaxPixelSize: maximumSize] as CFDictionary) { image = UIImage(cgImage: cg) }
            else { image = nil }
        } else {
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            generator.maximumSize = CGSize(width: 600, height: 600)
            generator.appliesPreferredTrackTransform = true
            if let cg = try? generator.copyCGImage(at: .zero, actualTime: nil) { image = UIImage(cgImage: cg) }
            else { image = nil }
        }
        if let image { images.setObject(image, forKey: key, cost: Int(image.size.width * image.size.height) * 4) }
        return image
    }
}

private struct CaptureThumbnail: View {
    let url: URL?
    let kind: SavedCaptureKind
    var maximumSize = 600
    @State private var image: UIImage?
    var body: some View {
        ZStack {
            HomeTheme.line
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: kind == .photo ? "photo" : "film").font(.title).foregroundStyle(HomeTheme.secondary) }
        }.clipped().task(id: url) {
            guard let url else { return }
            image = await CaptureThumbnails.shared.image(url: url, kind: kind, maximumSize: maximumSize)
        }
    }
}

private struct CaptureDetailView: View {
    let capture: SavedCapture
    @ObservedObject var library: HomeLibrary
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?
    @State private var confirmDelete = false
    @State private var zoom: CGFloat = 1
    private var current: SavedCapture { library.captures.first { $0.id == capture.id } ?? capture }
    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                VStack(spacing: 5) {
                    Text(current.cameraName).font(.headline)
                    Text(current.createdAt, format: .dateTime.month().day().hour().minute())
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                if let url = library.urls[current.id] {
                    if current.kind == .clip {
                        VideoPlayer(player: player).aspectRatio(4.0 / 3.0, contentMode: .fit)
                            .task(id: url) { player = AVPlayer(url: url); player?.play() }
                    } else {
                        CaptureThumbnail(url: url, kind: .photo, maximumSize: 2048).aspectRatio(4.0 / 3.0, contentMode: .fit)
                            .scaleEffect(zoom).clipped()
                            .gesture(MagnificationGesture().onChanged { zoom = min(4, max(1, $0)) }.onEnded { _ in zoom = 1 })
                    }
                } else { HomeEmptyState(symbol: "photo", title: "Moment unavailable", message: "This file could not be opened.") }
                Spacer(minLength: 0)
                Label("Saved on this iPhone", systemImage: "lock.shield").font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button { Task { await library.favorite(current) } } label: {
                        Label(current.favorite ? "Favorited" : "Favorite", systemImage: current.favorite ? "star.fill" : "star")
                    }.frame(maxWidth: .infinity, minHeight: 48)
                    if let url = library.urls[current.id] {
                        ShareLink(item: url) { Label("Share", systemImage: "square.and.arrow.up") }
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                    Button(role: .destructive) { confirmDelete = true } label: { Image(systemName: "trash").frame(width: 48, height: 48) }
                        .accessibilityLabel("Delete moment")
                }.font(.subheadline.weight(.semibold))
            }.padding(22).background(HomeTheme.canvas)
                .navigationTitle(current.kind == .photo ? "Snapshot" : "Video clip").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .confirmationDialog("Delete this moment?", isPresented: $confirmDelete, titleVisibility: .visible) {
                    Button("Delete", role: .destructive) {
                        Task { await library.remove(current); if !library.captures.contains(where: { $0.id == capture.id }) { dismiss() } }
                    }
                } message: { Text("The saved file will be removed from this iPhone.") }
                .onDisappear { player?.pause(); player = nil }
        }.tint(HomeTheme.forest)
    }
}
