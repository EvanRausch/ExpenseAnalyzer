import SwiftUI

/// Pinch to zoom, drag to pan once zoomed, a button to go back.
///
/// The content is laid out again at the zoomed size instead of being magnified, so text
/// stays sharp and labels appear as soon as there is room for them. Taps are handled here,
/// not by buttons inside the content: a one-finger tap reports its place in the content,
/// while two fingers always zoom.
struct ZoomableChart<Content: View>: View {
    var height: CGFloat
    var maximum: CGFloat = 10
    /// A one-finger tap, at a point in the content laid out at the given size.
    var onTap: (CGPoint, CGSize) -> Void = { _, _ in }
    /// The content, laid out to fill the given size; the rectangle is the part of it on
    /// screen, so labels can stay in view while the person pans.
    @ViewBuilder var content: (CGSize, CGRect) -> Content

    @State private var scale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var pinchStart: (scale: CGFloat, offset: CGSize)?
    @State private var dragStart: CGSize?

    var body: some View {
        GeometryReader { geometry in
            let viewport = geometry.size
            let size = CGSize(width: viewport.width * scale, height: viewport.height * scale)
            let shown = clamp(offset, content: size, in: viewport)
            let visible = CGRect(
                x: -shown.width, y: -shown.height, width: viewport.width, height: viewport.height)
            content(size, visible)
                .frame(width: size.width, height: size.height, alignment: .topLeading)
                .contentShape(Rectangle())
                // On the content itself, so the location is in its coordinates. A tap needs
                // one finger: two fingers fall through to the pinch below.
                .gesture(SpatialTapGesture().onEnded { onTap($0.location, size) })
                .offset(shown)
                .frame(width: viewport.width, height: viewport.height, alignment: .topLeading)
                .clipped()
                .contentShape(Rectangle())
                .gesture(magnify(in: viewport))
                // Panning only once zoomed, so the list underneath still scrolls otherwise
                .highPriorityGesture(pan(in: viewport), including: scale > 1 ? .all : .subviews)
                .overlay(alignment: .topTrailing) {
                    if scale > 1 {
                        Button {
                            withAnimation(.snappy) {
                                scale = 1
                                offset = .zero
                            }
                        } label: {
                            Image(systemName: "arrow.down.right.and.arrow.up.left")
                                .font(.caption.weight(.semibold))
                                .padding(7)
                                .background(.thinMaterial, in: Circle())
                        }
                        .buttonStyle(.plain)
                        .padding(6)
                        .accessibilityLabel("Reset zoom")
                    }
                }
        }
        .frame(height: height)
        .padding(.vertical, 4)
    }

    var isZoomed: Bool { scale > 1 }

    private func magnify(in viewport: CGSize) -> some Gesture {
        MagnifyGesture()
            .onChanged { value in
                let start = pinchStart ?? (scale, offset)
                pinchStart = start
                let next = min(max(start.scale * value.magnification, 1), maximum)
                // Keep the point under the fingers where it is
                let anchor = CGPoint(
                    x: value.startAnchor.x * viewport.width, y: value.startAnchor.y * viewport.height)
                let ratio = next / start.scale
                offset = CGSize(
                    width: anchor.x - (anchor.x - start.offset.width) * ratio,
                    height: anchor.y - (anchor.y - start.offset.height) * ratio)
                scale = next
            }
            .onEnded { _ in
                pinchStart = nil
                let size = CGSize(width: viewport.width * scale, height: viewport.height * scale)
                offset = clamp(offset, content: size, in: viewport)
            }
    }

    private func pan(in viewport: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 1)
            .onChanged { value in
                let start = dragStart ?? offset
                dragStart = start
                offset = CGSize(
                    width: start.width + value.translation.width,
                    height: start.height + value.translation.height)
            }
            .onEnded { _ in
                dragStart = nil
                let size = CGSize(width: viewport.width * scale, height: viewport.height * scale)
                withAnimation(.snappy) { offset = clamp(offset, content: size, in: viewport) }
            }
    }

    /// Keep the content covering the viewport.
    private func clamp(_ offset: CGSize, content: CGSize, in viewport: CGSize) -> CGSize {
        CGSize(
            width: min(0, max(viewport.width - content.width, offset.width)),
            height: min(0, max(viewport.height - content.height, offset.height)))
    }
}
