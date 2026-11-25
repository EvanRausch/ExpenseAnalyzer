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

    /// Lay out groups and their categories in `rect`; returns the clusters and the bubbles.
    static func layout(
        groups: [GroupTotal], categories: (String) -> [GroupTotal], in rect: CGRect
    ) -> (clusters: [Bubble], bubbles: [Bubble]) {
        // Radii in money units: area proportional to the amount
        var clusterRadius: [String: Double] = [:]
        var local: [String: [(name: String, total: Double, x: Double, y: Double, r: Double)]] = [:]
        for group in groups {
            let members = categories(group.name).filter { $0.total > 0 }
            let radii = members.map { (id: $0.name, r: sqrt($0.total)) }
            let spots = pack(radii, padding: 0.5)
            let placed = centred(
                members.map { m in
                    let p = spots[m.name] ?? .zero
                    return (p.x, p.y, sqrt(m.total))
                })
            local[group.name] = zip(members, placed).map { m, c in (m.name, m.total, c.x, c.y, c.r) }
            clusterRadius[group.name] = enclosing(placed) * 1.06 + 1
        }
        let clusterSpots = pack(
            groups.map { (id: $0.name, r: clusterRadius[$0.name] ?? 0) }, padding: 1)
        let all = centred(
            groups.map { group in
                let p = clusterSpots[group.name] ?? .zero
                return (p.x, p.y, clusterRadius[group.name] ?? 0)
            })
        let (minX, minY, maxX, maxY) = bounds(all)
        guard maxX > minX, maxY > minY else { return ([], []) }
        // Fill the available box, whatever shape the blob took
        let scale = min(rect.width / (maxX - minX), rect.height / (maxY - minY))
        let centre = CGPoint(x: rect.midX, y: rect.midY)

        var clusters: [Bubble] = []
        var bubbles: [Bubble] = []
        for (group, p) in zip(groups, all) {
            let cx = centre.x + p.x * scale, cy = centre.y + p.y * scale
            clusters.append(
                Bubble(
                    id: group.name, group: group.name, name: group.name, total: group.total,
                    x: cx, y: cy, r: (clusterRadius[group.name] ?? 0) * scale))
            for c in local[group.name] ?? [] {
                bubbles.append(
                    Bubble(
                        id: "\(group.name)/\(c.name)", group: group.name, name: c.name,
                        total: c.total, x: cx + c.x * scale, y: cy + c.y * scale, r: c.r * scale))
            }
        }
        return (clusters, bubbles)
    }
}

/// The bubble chart as a view: tap a bubble for its merchants, a cluster for its categories.
struct BubbleChartView: View {
    @Environment(DataStore.self) private var store
    @Environment(\.navigate) private var navigate
    let groups: [GroupTotal]
    let transactions: [Transaction]
    var height: CGFloat = 320

    var body: some View {
        GeometryReader { geometry in
            let rect = CGRect(origin: .zero, size: geometry.size).insetBy(dx: 4, dy: 4)
            let laid = Bubbles.layout(
                groups: groups,
                categories: { group in
                    transactions.filter { store.group(of: $0.category) == group }
                        .grouped(by: \.category)
                },
                in: rect)
            let colour = Dictionary(
                uniqueKeysWithValues: groups.enumerated().map { ($1.name, Palette.color($0)) })
            ZStack {
                ForEach(laid.clusters) { cluster in
                    Button { navigate(.categories(cluster.name)) } label: {
                        Circle().fill((colour[cluster.name] ?? .gray).opacity(0.15))
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
                            label(bubble)
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("chart")
                    .frame(width: bubble.r * 2, height: bubble.r * 2)
                    .position(x: bubble.x, y: bubble.y)
                    .accessibilityLabel("\(bubble.name), \(bubble.total.money)")
                }
            }
        }
        .frame(height: height)
        .padding(.vertical, 4)
    }

    @ViewBuilder private func label(_ bubble: Bubbles.Bubble) -> some View {
        if bubble.r > 34 {
            VStack(spacing: 1) {
                Text(bubble.name).font(.caption2.weight(.medium)).multilineTextAlignment(.center)
                    .lineLimit(2)
                Text(bubble.total.money).font(.caption2).monospacedDigit()
            }
            .foregroundStyle(.white)
            .padding(6)
            .frame(width: bubble.r * 1.8)
        } else if bubble.r > 13 {
            Text(store.emoji(for: bubble.name)).font(.system(size: min(bubble.r, 20)))
        }
    }
}
