import SwiftUI

/// Rectangles whose area is money: one per group, split into its categories.
///
/// Squarified layout (Bruls, Huizing & van Wijk), which keeps the rectangles close to
/// squares so that labels fit and small items stay visible.
extension CGRect {
    /// Like insetBy, but never the null rectangle (infinite origin): a rectangle too small
    /// to shrink becomes a point at its centre instead.
    func shrunk(by dx: CGFloat, _ dy: CGFloat) -> CGRect {
        CGRect(
            x: minX + min(dx, width / 2), y: minY + min(dy, height / 2),
            width: max(0, width - 2 * dx), height: max(0, height - 2 * dy))
    }

    var isFinite: Bool {
        origin.x.isFinite && origin.y.isFinite && width.isFinite && height.isFinite
    }
}

enum Treemap {
    struct Cell: Identifiable {
        let id: String
        let group: String
        let name: String
        let total: Double
        var count = 0
        let rect: CGRect
    }

    /// Place `items` (largest first) inside `rect`; areas are proportional to the values.
    static func layout(_ items: [(id: String, value: Double)], in rect: CGRect) -> [String: CGRect]
    {
        let total = items.reduce(0) { $0 + $1.value }
        guard total > 0, rect.width > 0, rect.height > 0 else { return [:] }
        let scale = rect.width * rect.height / total
        var scaled = items.map { ($0.id, $0.value * scale) }.filter { $0.1 > 0 }
        var remaining = rect
        var placed: [String: CGRect] = [:]
        var row: [(String, Double)] = []
        while !scaled.isEmpty {
            let side = min(remaining.width, remaining.height)
            let next = scaled[0]
            if row.isEmpty || worst(row, side) >= worst(row + [next], side) {
                row.append(next)
                scaled.removeFirst()
            } else {
                place(row, in: &remaining, into: &placed)
                row = []
            }
        }
        place(row, in: &remaining, into: &placed)
        return placed
    }

    /// The worst aspect ratio in a row laid along a side of length `side`.
    private static func worst(_ row: [(String, Double)], _ side: Double) -> Double {
        let sum = row.reduce(0) { $0 + $1.1 }
        guard sum > 0, side > 0 else { return .infinity }
        let areas = row.map(\.1)
        let biggest = areas.max()!, smallest = areas.min()!
        let s2 = sum * sum, w2 = side * side
        return max(w2 * biggest / s2, s2 / (w2 * smallest))
    }

    private static func place(
        _ row: [(String, Double)], in remaining: inout CGRect, into placed: inout [String: CGRect]
    ) {
        let sum = row.reduce(0) { $0 + $1.1 }
        guard sum > 0, remaining.width > 0, remaining.height > 0 else { return }
        if remaining.width >= remaining.height {
            // A column down the left edge
            let width = sum / remaining.height
            var y = remaining.minY
            for (id, area) in row {
                let height = area / width
                placed[id] = CGRect(x: remaining.minX, y: y, width: width, height: height)
                y += height
            }
            remaining = CGRect(
                x: remaining.minX + width, y: remaining.minY, width: remaining.width - width,
                height: remaining.height)
        } else {
            // A row along the top edge
            let height = sum / remaining.width
            var x = remaining.minX
            for (id, area) in row {
                let width = area / height
                placed[id] = CGRect(x: x, y: remaining.minY, width: width, height: height)
                x += width
            }
            remaining = CGRect(
                x: remaining.minX, y: remaining.minY + height, width: remaining.width,
                height: remaining.height - height)
        }
    }

    /// Groups laid out in `rect`, each subdivided into its categories.
    static func cells(
        groups: [GroupTotal], categories: (String) -> [GroupTotal], in rect: CGRect,
        gap: CGFloat = 3, header: CGFloat = 0
    ) -> (groups: [Cell], categories: [Cell]) {
        let outer = layout(groups.map { ($0.name, $0.total) }, in: rect)
        var groupCells: [Cell] = []
        var categoryCells: [Cell] = []
        for group in groups {
            guard let frame = outer[group.name] else { continue }
            groupCells.append(
                Cell(
                    id: group.name, group: group.name, name: group.name, total: group.total,
                    count: group.count, rect: frame))
            let padded = frame.shrunk(by: gap, gap)
            // The group's name sits in a strip at the top; categories fill the rest
            let inner = CGRect(
                x: padded.minX, y: padded.minY + header, width: padded.width,
                height: padded.height - header)
            guard inner.width > 0, inner.height > 0 else { continue }
            let members = categories(group.name)
            for (name, cellRect) in layout(members.map { ($0.name, $0.total) }, in: inner) {
                let member = members.first { $0.name == name }
                categoryCells.append(
                    Cell(
                        id: "\(group.name)/\(name)", group: group.name, name: name,
                        total: member?.total ?? 0, count: member?.count ?? 0,
                        rect: cellRect.shrunk(by: gap / 2, gap / 2)))
            }
        }
        return (groupCells, categoryCells)
    }
}

/// The treemap as a view: tap a category to see its merchants, a group's name to see its
/// categories; pinch to zoom in on the small ones.
struct TreemapView: View {
    @Environment(DataStore.self) private var store
    @Environment(\.navigate) private var navigate
    /// Largest first; the palette follows this order.
    let groups: [GroupTotal]
    let transactions: [Transaction]
    var height: CGFloat = 300

    private static let header: CGFloat = 18

    var body: some View {
        ZoomableChart(height: height, onTap: open) { size, visible in
            map(in: CGRect(origin: .zero, size: size), visible: visible)
        }
    }

    private func cells(in rect: CGRect) -> (groups: [Treemap.Cell], categories: [Treemap.Cell]) {
        Treemap.cells(
            groups: groups,
            categories: { group in
                transactions.filter { store.group(of: $0.category) == group }
                    .grouped(by: \.category)
            },
            in: rect, header: Self.header)
    }

    /// A one-finger tap: a category tile opens its merchants, a group's name strip its categories.
    private func open(at point: CGPoint, size: CGSize) {
        let cells = cells(in: CGRect(origin: .zero, size: size))
        if let cell = cells.categories.first(where: { $0.rect.contains(point) }) {
            navigate(.merchants(cell.name))
        } else if let group = cells.groups.first(where: { $0.rect.contains(point) }) {
            navigate(.categories(group.name))
        }
    }

    private func map(in rect: CGRect, visible: CGRect) -> some View {
        let cells = cells(in: rect)
        let colour = Dictionary(
            uniqueKeysWithValues: groups.enumerated().map { ($1.name, Palette.color($0)) })
        return ZStack(alignment: .topLeading) {
            ForEach(cells.groups) { cell in
                RoundedRectangle(cornerRadius: 6)
                    .fill((colour[cell.name] ?? .gray).opacity(0.18))
                    .frame(width: cell.rect.width, height: cell.rect.height)
                    .offset(x: cell.rect.minX, y: cell.rect.minY)
            }
            ForEach(cells.categories) { cell in
                categoryTile(cell, colour: colour[cell.group] ?? .gray, visible: visible)
                    .accessibilityIdentifier("chart")
                    .accessibilityAddTraits(.isButton)
                    .accessibilityAction { navigate(.merchants(cell.name)) }
                    .frame(width: cell.rect.width, height: cell.rect.height)
                    .offset(x: cell.rect.minX, y: cell.rect.minY)
            }
            ForEach(cells.groups) { cell in
                if cell.rect.width > 60 && cell.rect.height > Self.header * 2 {
                    let slide = Self.slide(cell.rect, toward: visible, labelHeight: Self.header)
                    Group {
                        HStack(spacing: 4) {
                            Text("\(store.emoji(for: cell.name)) \(cell.name)").lineLimit(1)
                            if cell.rect.width > 220 {
                                Text(cell.total.money).foregroundStyle(.secondary).monospacedDigit()
                            }
                        }
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 5)
                        .frame(height: Self.header)
                        .background(.thinMaterial, in: Capsule())
                        .frame(width: cell.rect.width - 6, height: Self.header, alignment: .leading)
                    }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("\(cell.name) group, \(cell.total.money)")
                    .accessibilityIdentifier("chart")
                    .accessibilityAddTraits(.isButton)
                    .accessibilityAction { navigate(.categories(cell.name)) }
                    .offset(x: cell.rect.minX + 3 + slide.width, y: cell.rect.minY + 1 + slide.height)
                }
            }
        }
        // Offsets do not count for layout: fill the rectangle so positions and taps agree
        .frame(width: rect.width, height: rect.height, alignment: .topLeading)
    }

    /// How far to move a label from a cell's top-left so it stays on screen when the cell is
    /// partly scrolled out of view (zoomed in), without leaving the cell.
    static func slide(_ cell: CGRect, toward visible: CGRect, labelHeight: CGFloat) -> CGSize {
        CGSize(
            width: min(max(0, visible.minX - cell.minX), max(0, cell.width - 80)),
            height: min(max(0, visible.minY - cell.minY), max(0, cell.height - labelHeight - 4)))
    }

    /// A tile label's slide, leaving room for the group's pinned name when both are pinned.
    static func tileSlide(_ cell: CGRect, toward visible: CGRect, labelHeight: CGFloat) -> CGSize {
        var slide = slide(cell, toward: visible, labelHeight: labelHeight)
        if slide.height > 0 {
            slide.height = min(slide.height + header + 2, max(0, cell.height - labelHeight - 4))
        }
        return slide
    }

    /// What fits in a tile: colour only, then the emoji, then name and amount, then the
    /// merchant count too, as the tile (or the zoom) grows.
    @ViewBuilder private func categoryTile(_ cell: Treemap.Cell, colour: Color, visible: CGRect)
        -> some View
    {
        let w = cell.rect.width, h = cell.rect.height
        let roomy = w > 70 && h > 40
        let large = w > 150 && h > 90
        let tiny = w < 34 || h < 24
        let slide = Self.tileSlide(cell.rect, toward: visible, labelHeight: large ? 54 : 36)
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 4).fill(colour.opacity(roomy ? 0.85 : 0.65))
            if !tiny {
                VStack(alignment: .leading, spacing: 1) {
                    Text(roomy ? "\(store.emoji(for: cell.name)) \(cell.name)" : store.emoji(for: cell.name))
                        .font(large ? .caption.weight(.semibold) : .caption2.weight(.medium))
                        .lineLimit(roomy ? 2 : 1)
                    if roomy {
                        Text(cell.total.money).font(.caption2).monospacedDigit()
                    }
                    if large {
                        Text("\(cell.count) transactions").font(.caption2).opacity(0.85)
                    }
                }
                .foregroundStyle(.white)
                .padding(4)
                .frame(maxWidth: max(0, w - slide.width), alignment: .leading)
                .offset(x: slide.width, y: slide.height)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(cell.name), \(cell.total.money)")
    }
}
