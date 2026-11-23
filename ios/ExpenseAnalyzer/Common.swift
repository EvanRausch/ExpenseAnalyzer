import Charts
import SwiftUI

extension Double {
    var money: String { formatted(.currency(code: "CAD").presentation(.narrow)) }

    var wholeMoney: String {
        formatted(.currency(code: "CAD").presentation(.narrow).precision(.fractionLength(0)))
    }

    var percent: String { formatted(.percent.precision(.fractionLength(1))) }
}

extension Color {
    init(hex: Int) {
        self.init(
            red: Double((hex >> 16) & 0xff) / 255,
            green: Double((hex >> 8) & 0xff) / 255,
            blue: Double(hex & 0xff) / 255)
    }

    // The Mac app's colours
    static let expense = Color(hex: 0xe65100)
    static let moneyIn = Color(hex: 0x2e7d32)
}

/// Colours for the largest groups in a breakdown; smaller ones share grey.
enum Palette {
    static let colors = [
        0x4e79a7, 0xf28e2b, 0xe15759, 0x76b7b2, 0x59a14f, 0xedc948, 0xb07aa1, 0xff9da7,
    ]
    .map(Color.init(hex:))

    static func color(_ index: Int) -> Color {
        index < colors.count ? colors[index] : .gray
    }
}

/// Donut of the largest groups, with everything past the palette folded into "Other".
struct ShareChart: View {
    /// Largest first.
    let groups: [GroupTotal]

    var body: some View {
        let slices = Self.slices(groups)
        Chart(Array(slices.enumerated()), id: \.element.id) { index, slice in
            SectorMark(
                angle: .value("Total", slice.total), innerRadius: .ratio(0.62), angularInset: 1
            )
            .foregroundStyle(Palette.color(index))
        }
        .chartLegend(.hidden)
        .overlay {
            VStack(spacing: 2) {
                Text("Total").font(.caption).foregroundStyle(.secondary)
                Text(groups.reduce(0) { $0 + $1.total }.money).font(.headline).monospacedDigit()
            }
        }
        .frame(height: 210)
        .padding(.vertical, 8)
    }

    static func slices(_ groups: [GroupTotal]) -> [GroupTotal] {
        let top = Array(groups.prefix(Palette.colors.count))
        let rest = groups.dropFirst(Palette.colors.count)
        guard !rest.isEmpty else { return top }
        return top + [
            GroupTotal(name: "Other", total: rest.reduce(0) { $0 + $1.total }, count: rest.count)
        ]
    }
}

/// One line of a breakdown: name, optional explanation, total and share.
struct GroupRow: View {
    let title: String
    var subtitle = ""
    let group: GroupTotal
    let grandTotal: Double
    var color: Color?

    var body: some View {
        HStack(spacing: 10) {
            if let color {
                Circle().fill(color).frame(width: 10, height: 10)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if !subtitle.isEmpty {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(group.total.money).monospacedDigit()
                Text(
                    "\(group.count) · \(grandTotal > 0 ? (group.total / grandTotal).percent : "–")"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
    }
}

struct TransactionRow: View {
    let transaction: Transaction
    let emoji: String

    var body: some View {
        HStack(spacing: 10) {
            Text(emoji).font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(transaction.merchant).lineLimit(1)
                Text(
                    transaction.summary.isEmpty ? transaction.bankDescription : transaction.summary
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                AmountText(amount: transaction.amount)
                Text(transaction.date.formatted(date: .abbreviated, time: .omitted))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Money out in orange with a minus sign, money in in green with a plus sign, as on the Mac.
struct AmountText: View {
    let amount: Double

    var body: some View {
        Text((amount < 0 ? "+" : "-") + abs(amount).money)
            .monospacedDigit()
            .foregroundStyle(amount < 0 ? Color.moneyIn : Color.expense)
    }
}

/// Account and date choices shared by the Summary, Transactions and Income tabs.
struct FilterBar: View {
    @Binding var filter: TransactionFilter
    /// The dates on offer come from these.
    let transactions: [Transaction]
    @State private var choosingRange = false
    @State private var from = Date.now
    @State private var to = Date.now

    var body: some View {
        HStack {
            Menu {
                Picker("Account", selection: $filter.account) {
                    ForEach(AccountFilter.allCases) { Text($0.rawValue.capitalized).tag($0) }
                }
            } label: {
                Label(filter.account.rawValue.capitalized, systemImage: "creditcard")
            }
            Spacer()
            Menu {
                Button("All dates") { filter.period = .all }
                Menu("Year") {
                    ForEach(years, id: \.self) { year in
                        Button(String(year)) { filter.period = .year(year) }
                    }
                }
                Menu("Month") {
                    ForEach(years, id: \.self) { year in
                        Menu(String(year)) {
                            ForEach(months(in: year), id: \.self) { month in
                                Button(month.formatted(.dateTime.month(.wide))) {
                                    filter.period = .month(month)
                                }
                            }
                        }
                    }
                }
                Button("Custom range…") {
                    let dates = transactions.map(\.date)
                    from = dates.min() ?? .now
                    to = dates.max() ?? .now
                    choosingRange = true
                }
            } label: {
                Label(filter.periodLabel, systemImage: "calendar")
            }
        }
        .font(.subheadline)
        .sheet(isPresented: $choosingRange) {
            NavigationStack {
                Form {
                    DatePicker("From", selection: $from, displayedComponents: .date)
                    DatePicker("To", selection: $to, displayedComponents: .date)
                }
                .navigationTitle("Date Range")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { choosingRange = false }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Apply") {
                            filter.period = .range(min(from, to), max(from, to))
                            choosingRange = false
                        }
                    }
                }
            }
            .presentationDetents([.medium])
        }
    }

    private var years: [Int] {
        Set(transactions.map { Calendar.current.component(.year, from: $0.date) }).sorted(by: >)
    }

    private func months(in year: Int) -> [Date] {
        let calendar = Calendar.current
        let starts = transactions.compactMap {
            calendar.dateInterval(of: .month, for: $0.date)?.start
        }
        return Set(starts.filter { calendar.component(.year, from: $0) == year }).sorted(by: >)
    }
}

/// A figure with its label, for the grids of statistics.
struct StatRow: View {
    let label: String
    let value: String

    init(_ label: String, _ value: String) {
        self.label = label
        self.value = value
    }

    var body: some View {
        LabeledContent(label) {
            Text(value).monospacedDigit().multilineTextAlignment(.trailing)
        }
    }
}
