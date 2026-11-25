import SwiftUI

/// Rectangles whose area is money: one per group, split into its categories.
///
/// Squarified layout (Bruls, Huizing & van Wijk), which keeps the rectangles close to
/// squares so that labels fit and small items stay visible.
enum Treemap {
    struct Cell: Identifiable {
        let id: String
        let group: String
        let name: String
        let total: Double
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
        guard sum > 0 else { return }
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
                Cell(id: group.name, group: group.name, name: group.name, total: group.total, rect: frame))
            let padded = frame.insetBy(dx: gap, dy: gap)
            // The group's name sits in a strip at the top; categories fill the rest
            let inner = CGRect(
                x: padded.minX, y: padded.minY + header, width: padded.width,
                height: padded.height - header)
            guard inner.width > 0, inner.height > 0 else { continue }
            let members = categories(group.name)
            for (name, cellRect) in layout(members.map { ($0.name, $0.total) }, in: inner) {
                let total = members.first { $0.name == name }?.total ?? 0
                categoryCells.append(
                    Cell(
                        id: "\(group.name)/\(name)", group: group.name, name: name, total: total,
                        rect: cellRect.insetBy(dx: gap / 2, dy: gap / 2)))
            }
        }
        return (groupCells, categoryCells)
    }
}

/// The treemap as a view: tap a category to see its merchants, a group's name to see its categories.
struct TreemapView: View {
    @Environment(DataStore.self) private var store
    @Environment(\.navigate) private var navigate
    /// Largest first; the palette follows this order.
    let groups: [GroupTotal]
    let transactions: [Transaction]
    var height: CGFloat = 300

    private static let header: CGFloat = 18

    var body: some View {
        GeometryReader { geometry in
            let rect = CGRect(origin: .zero, size: geometry.size)
            let cells = Treemap.cells(
                groups: groups,
                categories: { group in
                    transactions.filter { store.group(of: $0.category) == group }
                        .grouped(by: \.category)
                },
                in: rect, header: Self.header)
            let colour = Dictionary(
                uniqueKeysWithValues: groups.enumerated().map { ($1.name, Palette.color($0)) })
            ZStack(alignment: .topLeading) {
                ForEach(cells.groups) { cell in
                    RoundedRectangle(cornerRadius: 6)
                        .fill((colour[cell.name] ?? .gray).opacity(0.18))
                        .frame(width: cell.rect.width, height: cell.rect.height)
                        .offset(x: cell.rect.minX, y: cell.rect.minY)
                }
                ForEach(cells.categories) { cell in
                    Button { navigate(.merchants(cell.name)) } label: {
                        categoryTile(cell, colour: colour[cell.group] ?? .gray)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("chart")
                    .frame(width: cell.rect.width, height: cell.rect.height)
                    .offset(x: cell.rect.minX, y: cell.rect.minY)
                }
                ForEach(cells.groups) { cell in
                    if cell.rect.width > 60 && cell.rect.height > Self.header * 2 {
                        Button { navigate(.categories(cell.name)) } label: {
                            Text("\(store.emoji(for: cell.name)) \(cell.name)")
                                .font(.caption2.weight(.semibold))
                                .lineLimit(1)
                                .padding(.horizontal, 5)
                                .frame(width: cell.rect.width - 6, height: Self.header, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("chart")
                        .offset(x: cell.rect.minX + 3, y: cell.rect.minY + 1)
                    }
                }
            }
        }
        .frame(height: height)
        .padding(.vertical, 4)
    }

    @ViewBuilder private func categoryTile(_ cell: Treemap.Cell, colour: Color) -> some View {
        let roomy = cell.rect.width > 70 && cell.rect.height > 40
        let tiny = cell.rect.width < 34 || cell.rect.height < 24
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 4).fill(colour.opacity(roomy ? 0.85 : 0.65))
            if !tiny {
                VStack(alignment: .leading, spacing: 1) {
                    Text(roomy ? cell.name : store.emoji(for: cell.name))
                        .font(.caption2.weight(.medium))
                        .lineLimit(roomy ? 2 : 1)
                    if roomy {
                        Text(cell.total.money).font(.caption2).monospacedDigit()
                    }
                }
                .foregroundStyle(.white)
                .padding(4)
            }
        }
        .accessibilityLabel("\(cell.name), \(cell.total.money)")
    }
}
