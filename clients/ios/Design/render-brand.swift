// Original Nxrs Home vector geometry. Regenerate with: swift Design/render-brand.swift <output.png>
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let size = 1024
let output = CommandLine.arguments[1]
let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
                        bytesPerRow: size * 4, space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
context.translateBy(x: 0, y: CGFloat(size)); context.scaleBy(x: 1, y: -1)
func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> CGColor {
    CGColor(red: r / 255, green: g / 255, blue: b / 255, alpha: 1)
}
context.setFillColor(color(20, 51, 40)); context.fill(CGRect(x: 0, y: 0, width: size, height: size))
context.setStrokeColor(color(247, 248, 239)); context.setLineWidth(46)
context.setLineCap(.round); context.setLineJoin(.round)
context.move(to: CGPoint(x: 236, y: 471)); context.addLine(to: CGPoint(x: 512, y: 236))
context.addLine(to: CGPoint(x: 788, y: 471)); context.addLine(to: CGPoint(x: 788, y: 737))
context.addQuadCurve(to: CGPoint(x: 706, y: 819), control: CGPoint(x: 788, y: 819))
context.addLine(to: CGPoint(x: 318, y: 819))
context.addQuadCurve(to: CGPoint(x: 236, y: 737), control: CGPoint(x: 236, y: 819))
context.closePath(); context.strokePath()
context.setStrokeColor(color(214, 240, 163)); context.strokeEllipse(in: CGRect(x: 384, y: 445, width: 256, height: 256))
context.setFillColor(color(214, 240, 163)); context.fillEllipse(in: CGRect(x: 535, y: 504, width: 46, height: 46))
let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: output) as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, context.makeImage()!, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Icon could not be written") }
print("Rendered original 1024px opaque Nxrs Home icon")
