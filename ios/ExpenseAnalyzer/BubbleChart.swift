import SwiftUI

/// Circles whose area is money: one bubble per category, clustered by group.
///
/// Bubbles are placed greedily on a spiral around the cluster's centre (largest first,
/// each as close to the centre as it fits), then the clusters are placed the same way.
enum Bubbles {
    struct Bubble: Identifiable {
        let id: String
        let group: String
        let name: String
        let total: Double
        var count = 0
        var x: Double
        var y: Double
        var r: Double
    }

    /// Pack circles (largest first) around the origin; returns id -> (x, y).
    static func pack(_ items: [(id: String, r: Double)], padding: Double = 0) -> [String: CGPoint] {
        var placed: [(x: Double, y: Double, r: Double)] = []
        var result: [String: CGPoint] = [:]
        for (index, item) in items.enumerated() {
            if index == 0 {
                placed.append((0, 0, item.r))
                result[item.id] = .zero
                continue
            }
            var best: (x: Double, y: Double)?
            var distance = 0.0
            let step = max(item.r / 4, 0.5)
            // Spiral out until a free spot is found; sample angles densely
            while best == nil && distance < 1e5 {
                distance += step
                for k in 0..<48 {
                    let angle = Double(k) / 48 * 2 * .pi + distance * 0.1
                    let x = cos(angle) * distance, y = sin(angle) * distance
                    let clear = placed.allSatisfy {
                        hypot($0.x - x, $0.y - y) >= $0.r + item.r + padding
                    }
                    if clear { best = (x, y); break }
                }
            }
            let spot = best ?? (distance, 0)
            placed.append((spot.x, spot.y, item.r))
            result[item.id] = CGPoint(x: spot.x, y: spot.y)
        }
        return result
    }

    /// Radius of the smallest origin-centred circle holding all of these.
    static func enclosing(_ circles: [(x: Double, y: Double, r: Double)]) -> Double {
        circles.map { hypot($0.x, $0.y) + $0.r }.max() ?? 0
    }

    /// The box around these circles: (minX, minY, maxX, maxY).
    static func bounds(_ circles: [(x: Double, y: Double, r: Double)]) -> (Double, Double, Double, Double) {
        (
            circles.map { $0.x - $0.r }.min() ?? 0, circles.map { $0.y - $0.r }.min() ?? 0,
            circles.map { $0.x + $0.r }.max() ?? 0, circles.map { $0.y + $0.r }.max() ?? 0
        )
    }

    /// Move circles so that the middle of their box is the origin.
    static func centred(_ circles: [(x: Double, y: Double, r: Double)]) -> [(x: Double, y: Double, r: Double)] {
        let (minX, minY, maxX, maxY) = bounds(circles)
        let dx = (minX + maxX) / 2, dy = (minY + maxY) / 2
        return circles.map { ($0.x - dx, $0.y - dy, $0.r) }
    }

    /// A packed layout in money units, centred on the origin, before fitting it to a view.
    struct Unit {
        var clusters: [Bubble] = []
        var bubbles: [Bubble] = []
        var bounds = (minX: 0.0, minY: 0.0, maxX: 0.0, maxY: 0.0)
    }

    /// Pack the categories of each group, then the groups. Independent of the view's size,
    /// so it can be computed once and fitted many times (zooming).
    static func unitLayout(groups: [GroupTotal], categories: (String) -> [GroupTotal]) -> Unit {
        var unit = Unit()
        var clusterRadius: [String: Double] = [:]
        var local: [String: [Bubble]] = [:]
        for group in groups {
            let members = categories(group.name).filter { $0.total > 0 }
            let spots = pack(members.map { (id: $0.name, r: sqrt($0.total)) }, padding: 0.5)
            let placed = centred(
                members.map { m in
                    let p = spots[m.name] ?? .zero
                    return (p.x, p.y, sqrt(m.total))
                })
            local[group.name] = zip(members, placed).map { m, c in
                Bubble(
                    id: "\(group.name)/\(m.name)", group: group.name, name: m.name, total: m.total,
                    count: m.count, x: c.x, y: c.y, r: c.r)
            }
            clusterRadius[group.name] = enclosing(placed) * 1.06 + 1
        }
        let clusterSpots = pack(
            groups.map { (id: $0.name, r: clusterRadius[$0.name] ?? 0) }, padding: 1)
        let all = centred(
            groups.map { group in
                let p = clusterSpots[group.name] ?? .zero
                return (p.x, p.y, clusterRadius[group.name] ?? 0)
            })
        for (group, p) in zip(groups, all) {
            unit.clusters.append(
                Bubble(
                    id: group.name, group: group.name, name: group.name, total: group.total,
                    count: group.count, x: p.x, y: p.y, r: p.r))
            for var bubble in local[group.name] ?? [] {
                bubble.x += p.x
                bubble.y += p.y
                unit.bubbles.append(bubble)
            }
        }
        unit.bounds = bounds(all)
        return unit
    }

    /// The unit layout scaled and moved to fill `rect`.
    static func fit(_ unit: Unit, in rect: CGRect) -> (clusters: [Bubble], bubbles: [Bubble]) {
        let (minX, minY, maxX, maxY) = unit.bounds
        guard maxX > minX, maxY > minY else { return ([], []) }
        let scale = min(rect.width / (maxX - minX), rect.height / (maxY - minY))
        func place(_ b: Bubble) -> Bubble {
            var out = b
            out.x = rect.midX + b.x * scale
            out.y = rect.midY + b.y * scale
            out.r = b.r * scale
            return out
        }
        return (unit.clusters.map(place), unit.bubbles.map(place))
    }
}

/// The bubble chart as a view: tap a bubble for its merchants, a cluster for its categories;
/// pinch to zoom in on the small ones.
struct BubbleChartView: View {
    @Environment(DataStore.self) private var store
    @Environment(\.navigate) private var navigate
    let groups: [GroupTotal]
    let transactions: [Transaction]
    var height: CGFloat = 320

    @State private var unit = Bubbles.Unit()

    /// Changes whenever the amounts do, so the packing is redone only then
    private var signature: String {
        groups.map { "\($0.name)=\($0.total)" }.joined(separator: ";") + "|\(transactions.count)"
    }

    var body: some View {
        ZoomableChart(height: height) { size, visible in
            chart(in: CGRect(origin: .zero, size: size).insetBy(dx: 4, dy: 4), visible: visible)
        }
        .onChange(of: signature, initial: true) { _, _ in
            unit = Bubbles.unitLayout(
                groups: groups,
                categories: { group in
                    transactions.filter { store.group(of: $0.category) == group }
                        .grouped(by: \.category)
                })
        }
    }

    private func chart(in rect: CGRect, visible: CGRect) -> some View {
        let laid = Bubbles.fit(unit, in: rect)
        let colour = Dictionary(
            uniqueKeysWithValues: groups.enumerated().map { ($1.name, Palette.color($0)) })
        return ZStack {
            ForEach(laid.clusters) { cluster in
                Button { navigate(.categories(cluster.name)) } label: {
                    ZStack(alignment: .top) {
                        Circle().fill((colour[cluster.name] ?? .gray).opacity(0.15))
                        if cluster.r > 90 {
                            Text("\(store.emoji(for: cluster.name)) \(cluster.name)")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.secondary)
                                .padding(.top, 6)
                        }
                    }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("chart")
                .frame(width: cluster.r * 2, height: cluster.r * 2)
                .position(x: cluster.x, y: cluster.y)
                .accessibilityLabel("\(cluster.name) group, \(cluster.total.money)")
            }
            ForEach(laid.bubbles) { bubble in
                Button { navigate(.merchants(bubble.name)) } label: {
                    ZStack {
                        Circle().fill(colour[bubble.group] ?? .gray)
                        label(bubble).offset(Self.slide(bubble, toward: visible))
                    }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("chart")
                .frame(width: bubble.r * 2, height: bubble.r * 2)
                .position(x: bubble.x, y: bubble.y)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(bubble.name), \(bubble.total.money)")
            }
        }
    }

    /// Move a bubble's label toward the middle of what is on screen when the bubble is
    /// larger than the view, staying well inside the circle.
    static func slide(_ bubble: Bubbles.Bubble, toward visible: CGRect) -> CGSize {
        let reach = bubble.r * 0.45
        let dx = min(max(visible.midX - bubble.x, -reach), reach)
        let dy = min(max(visible.midY - bubble.y, -reach), reach)
        // Only when the bubble actually overflows the view
        guard bubble.r * 2 > min(visible.width, visible.height) else { return .zero }
        return CGSize(width: dx, height: dy)
    }

    /// What fits in a bubble as it (or the zoom) grows: emoji, then name and amount, then
    /// the transaction count.
    @ViewBuilder private func label(_ bubble: Bubbles.Bubble) -> some View {
        if bubble.r > 34 {
            VStack(spacing: 1) {
                Text(store.emoji(for: bubble.name)).font(.system(size: min(bubble.r / 2.5, 28)))
                Text(bubble.name)
                    .font(bubble.r > 60 ? .caption.weight(.semibold) : .caption2.weight(.medium))
                    .multilineTextAlignment(.center)
                    .lineLimit(bubble.r > 60 ? 3 : 2)
                Text(bubble.total.money).font(.caption2).monospacedDigit()
                if bubble.r > 60 {
                    Text("\(bubble.count) transactions").font(.caption2).opacity(0.85)
                }
            }
            .foregroundStyle(.white)
            .padding(6)
            .frame(width: bubble.r * 1.7)
        } else if bubble.r > 13 {
            Text(store.emoji(for: bubble.name)).font(.system(size: min(bubble.r, 20)))
        }
    }
}
