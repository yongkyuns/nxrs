import SwiftUI

enum HomeTheme {
    static let canvas = adaptive(0xF6F5F0, 0x101916)
    static let surface = adaptive(0xFFFFFF, 0x1B2722)
    static let ink = adaptive(0x193D32, 0xF2F5EC)
    static let secondary = adaptive(0x64736B, 0xA2B4A8)
    static let forest = adaptive(0x234E3F, 0xC9E9BE)
    static let lime = Color(red: 0.84, green: 0.94, blue: 0.64)
    static let line = adaptive(0xE3E8DE, 0x324039)
    static let warning = adaptive(0x8B4D18, 0xF0BC7C)
    static let darkForest = Color(red: 0.08, green: 0.20, blue: 0.16)

    private static func adaptive(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(uiColor: UIColor { traits in
            let value = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: CGFloat((value >> 16) & 255) / 255,
                           green: CGFloat((value >> 8) & 255) / 255,
                           blue: CGFloat(value & 255) / 255, alpha: 1)
        })
    }
}

/// Original vector identity: a shelter around a camera lens.
struct HomeMark: View {
    var light = false
    var body: some View {
        GeometryReader { geometry in
            let size = min(geometry.size.width, geometry.size.height)
            ZStack {
                RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
                    .fill(light ? HomeTheme.lime : HomeTheme.darkForest)
                Path { path in
                    path.move(to: CGPoint(x: size * 0.23, y: size * 0.46))
                    path.addLine(to: CGPoint(x: size * 0.50, y: size * 0.23))
                    path.addLine(to: CGPoint(x: size * 0.77, y: size * 0.46))
                    path.addLine(to: CGPoint(x: size * 0.77, y: size * 0.72))
                    path.addQuadCurve(to: CGPoint(x: size * 0.69, y: size * 0.80),
                                      control: CGPoint(x: size * 0.77, y: size * 0.80))
                    path.addLine(to: CGPoint(x: size * 0.31, y: size * 0.80))
                    path.addQuadCurve(to: CGPoint(x: size * 0.23, y: size * 0.72),
                                      control: CGPoint(x: size * 0.23, y: size * 0.80))
                    path.closeSubpath()
                }.stroke(light ? HomeTheme.darkForest : Color.white,
                         style: StrokeStyle(lineWidth: size * 0.045, lineCap: .round, lineJoin: .round))
                Circle().stroke(light ? HomeTheme.darkForest : HomeTheme.lime, lineWidth: size * 0.045)
                    .frame(width: size * 0.25, height: size * 0.25).offset(y: size * 0.06)
                Circle().fill(light ? HomeTheme.darkForest : HomeTheme.lime)
                    .frame(width: size * 0.045, height: size * 0.045)
                    .offset(x: size * 0.045, y: size * 0.015)
            }
            .frame(width: size, height: size)
        }.accessibilityHidden(true)
    }
}

struct HomeWordmark: View {
    var body: some View {
        HStack(spacing: 10) {
            HomeMark().frame(width: 34, height: 34)
            Text("nxrs").font(.system(.title2, design: .rounded).weight(.bold))
                .tracking(-0.8).foregroundStyle(HomeTheme.ink)
            Text("HOME").font(.caption2.weight(.semibold)).tracking(2.6)
                .foregroundStyle(HomeTheme.secondary).padding(.leading, 2)
        }.dynamicTypeSize(...DynamicTypeSize.xxxLarge)
            .accessibilityElement(children: .ignore).accessibilityLabel("Nxrs Home")
    }
}

struct PrimaryHomeButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.headline).frame(maxWidth: .infinity).padding(.vertical, 17)
            .foregroundStyle(HomeTheme.darkForest)
            .background(HomeTheme.lime, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .opacity(configuration.isPressed ? 0.75 : 1)
    }
}

struct HomeIconButton: View {
    let symbol: String
    let label: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 18, weight: .semibold))
                .frame(width: 46, height: 46)
                .background(HomeTheme.surface, in: Circle())
                .overlay(Circle().strokeBorder(HomeTheme.line, lineWidth: 1))
        }.buttonStyle(.plain).foregroundStyle(HomeTheme.ink).accessibilityLabel(label)
    }
}

struct HomeSectionHeading: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    let title: String
    var detail: String? = nil
    var body: some View {
        (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 6)) : AnyLayout(HStackLayout(alignment: .firstTextBaseline))) {
            Text(title).font(.title3.weight(.bold)).foregroundStyle(HomeTheme.ink)
            if !typeSize.isAccessibilitySize { Spacer() }
            if let detail { Text(detail).font(.caption).foregroundStyle(HomeTheme.secondary) }
        }
    }
}

struct HomeEmptyState: View {
    let symbol: String
    let title: String
    let message: String
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol).font(.system(size: 30, weight: .light))
                .frame(width: 76, height: 76).background(HomeTheme.forest.opacity(0.08), in: Circle())
            Text(title).font(.title3.weight(.bold))
            Text(message).font(.subheadline).foregroundStyle(HomeTheme.secondary)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
        }.foregroundStyle(HomeTheme.ink).padding(28).frame(maxWidth: .infinity)
    }
}

/// Editorial illustration, used for setup and honest camera placeholders, never as live footage.
struct HomeRoomIllustration: View {
    var night = false
    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            let height = geometry.size.height
            ZStack {
                (night ? Color(red: 0.14, green: 0.24, blue: 0.21) : Color(red: 0.89, green: 0.91, blue: 0.83))
                Circle().fill(Color.white.opacity(night ? 0.05 : 0.3))
                    .frame(width: width * 0.9).offset(x: width * 0.25, y: -height * 0.2)
                RoundedRectangle(cornerRadius: 35).fill(Color(red: 0.70, green: 0.79, blue: 0.67))
                    .frame(width: width * 0.28, height: height * 0.50).offset(x: width * 0.22, y: -height * 0.09)
                Rectangle().fill(Color.white.opacity(0.5)).frame(width: 2, height: height * 0.5)
                    .offset(x: width * 0.22, y: -height * 0.09)
                Rectangle().fill(Color.white.opacity(0.5)).frame(width: width * 0.28, height: 2)
                    .offset(x: width * 0.22, y: -height * 0.09)
                Ellipse().fill(HomeTheme.darkForest.opacity(0.08))
                    .frame(width: width * 0.72, height: height * 0.11).offset(y: height * 0.32)
                RoundedRectangle(cornerRadius: 25).fill(Color(red: 0.96, green: 0.92, blue: 0.80))
                    .frame(width: width * 0.57, height: height * 0.23).offset(x: -width * 0.04, y: height * 0.14)
                RoundedRectangle(cornerRadius: 18).fill(Color(red: 0.90, green: 0.85, blue: 0.70))
                    .frame(width: width * 0.63, height: height * 0.13).offset(x: -width * 0.04, y: height * 0.26)
                RoundedRectangle(cornerRadius: 10).fill(Color(red: 0.54, green: 0.65, blue: 0.46))
                    .frame(width: width * 0.13, height: height * 0.14).rotationEffect(.degrees(-9))
                    .offset(x: -width * 0.19, y: height * 0.1)
                Image(systemName: "leaf.fill").font(.system(size: width * 0.15, weight: .light))
                    .rotationEffect(.degrees(-30)).foregroundStyle(Color(red: 0.24, green: 0.43, blue: 0.32))
                    .offset(x: -width * 0.34, y: -height * 0.03)
                RoundedRectangle(cornerRadius: 6).fill(Color(red: 0.71, green: 0.57, blue: 0.43))
                    .frame(width: width * 0.10, height: height * 0.13).offset(x: -width * 0.34, y: height * 0.13)
            }.clipped()
        }.accessibilityHidden(true)
    }
}
