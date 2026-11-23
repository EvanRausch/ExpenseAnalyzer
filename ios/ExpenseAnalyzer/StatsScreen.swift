import Charts
import SwiftUI

/// Charts for one group of transactions, matching the Mac app's statistics window.
struct StatsScreen: View {
    let title: String
    /// Income is charted payment by payment, spending month by month.
    let isIncome: Bool
    private let stats: DetailStats

    init(title: String, transactions: [Transaction], isIncome: Bool) {
        self.title = title
        self.isIncome = isIncome
        stats = DetailStats(transactions)
    }

    private var tint: Color { isIncome ? .moneyIn : .expense }
    private var count: Int { stats.transactions.count }

    var body: some View {
        List {
            if stats.transactions.isEmpty {
                ContentUnavailableView("No transactions", systemImage: "chart.bar")
            } else {
                amounts
                weekdays
                sizes
                cumulative
                if !isIncome && stats.monthly.count >= 2 { monthChanges }
                if !isIncome && stats.years.count >= 2 { years }
                if isIncome && !stats.gaps.isEmpty { intervals }
            }
        }
        .navigationTitle("\(title) Stats")
    }

    @ViewBuilder private var amounts: some View {
        if isIncome {
            Section("Payments") {
                Chart(stats.transactions) { payment in
                    BarMark(
                        x: .value("Date", payment.date), y: .value("Amount", payment.size),
                        width: .fixed(5)
                    )
                    .foregroundStyle(tint)
                }
                .moneyChart()
                StatRow("Total", stats.total.money)
                StatRow("Payments", "\(count)")
                StatRow("Average", (stats.total / Double(count)).money)
                if let largest = stats.transactions.largest {
                    StatRow(
                        "Largest",
                        "\(largest.size.money) · \(largest.date.formatted(date: .abbreviated, time: .omitted))"
                    )
                }
            }
        } else {
            Section("Monthly Spending") {
                Chart(stats.monthly) { month in
                    BarMark(
                        x: .value("Month", month.month, unit: .month),
                        y: .value("Total", month.total)
                    )
                    .foregroundStyle(tint)
                }
                .moneyChart()
                StatRow("Total", stats.total.money)
                StatRow("Monthly average", (stats.total / Double(stats.monthly.count)).money)
                if let peak = stats.monthly.max(by: { $0.total < $1.total }) {
                    StatRow("Peak", "\(peak.total.money) · \(peak.label)")
                }
            }
        }
    }

    private var weekdays: some View {
        Section("Day of Week") {
            Chart(stats.weekdays) { day in
                BarMark(x: .value("Day", String(day.label.prefix(3))), y: .value("Total", day.sum))
                    .foregroundStyle(Color(hex: 0x5c6bc0))
            }
            .moneyChart()
            let active = stats.weekdays.filter { $0.count > 0 }
            if let busiest = active.max(by: { $0.sum < $1.sum }),
                let quietest = active.min(by: { $0.sum < $1.sum })
            {
                StatRow("Busiest", "\(busiest.label) · \(busiest.sum.money)")
                StatRow("Quietest", "\(quietest.label) · \(quietest.sum.money)")
            }
        }
    }

    private var sizes: some View {
        Section("Transaction Size") {
            BucketBars(buckets: stats.sizes, color: Color(hex: 0x00838f))
            if let common = stats.sizes.max(by: { $0.count < $1.count }) {
                StatRow("Most common", "\(common.label) · \(common.count)")
            }
            StatRow("Median", stats.median.money)
            StatRow("Transactions", "\(count)")
        }
    }

    private var cumulative: some View {
        Section("Cumulative") {
            Chart {
                ForEach(stats.cumulative) { point in
                    LineMark(
                        x: .value("Date", point.date), y: .value("Total", point.value),
                        series: .value("Line", "Total")
                    )
                    .foregroundStyle(tint)
                }
                // What a steady pace from the first day to the last would look like
                if let first = stats.cumulative.first, let last = stats.cumulative.last,
                    first.date < last.date
                {
                    ForEach([DetailStats.Point(date: first.date, value: 0), last]) { point in
                        LineMark(
                            x: .value("Date", point.date), y: .value("Total", point.value),
                            series: .value("Line", "Average rate")
                        )
                        .foregroundStyle(.gray)
                        .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    }
                }
            }
            .moneyChart()
            StatRow("Total", stats.total.money)
            StatRow("Transactions", "\(count)")
            StatRow("Average per transaction", (stats.total / Double(count)).money)
        }
    }

    private var monthChanges: some View {
        Section("Month-over-Month Change") {
            Chart(stats.monthChanges) { change in
                BarMark(
                    x: .value("Month", change.date, unit: .month), y: .value("Change", change.value)
                )
                .foregroundStyle(Color(hex: 0x7e57c2))
            }
            .chartYAxis {
                AxisMarks { value in
                    AxisGridLine()
                    AxisValueLabel {
                        if let percent = value.as(Double.self) { Text("\(Int(percent))%") }
                    }
                }
            }
            .frame(height: 180)
            if let up = stats.monthChanges.max(by: { $0.value < $1.value }), up.value > 0 {
                StatRow(
                    "Biggest increase", "+\(Int(up.value.rounded()))% · \(Self.monthLabel(up.date))"
                )
            }
            if let down = stats.monthChanges.min(by: { $0.value < $1.value }), down.value < 0 {
                StatRow(
                    "Biggest drop", "\(Int(down.value.rounded()))% · \(Self.monthLabel(down.date))")
            }
        }
    }

    private var years: some View {
        Section("Year over Year") {
            Chart(stats.yearMonths) { month in
                LineMark(x: .value("Month", month.month), y: .value("Total", month.total))
                    .foregroundStyle(by: .value("Year", String(month.year)))
                PointMark(x: .value("Month", month.month), y: .value("Total", month.total))
                    .foregroundStyle(by: .value("Year", String(month.year)))
            }
            .chartXScale(domain: 1...12)
            .chartXAxis {
                AxisMarks(values: [1, 4, 7, 10]) { value in
                    AxisGridLine()
                    AxisValueLabel {
                        if let month = value.as(Int.self) {
                            Text(Calendar.current.shortMonthSymbols[month - 1])
                        }
                    }
                }
            }
            .moneyChart()
            let totals = stats.years.map { year in
                (year, stats.yearMonths.filter { $0.year == year }.reduce(0) { $0 + $1.total })
            }
            ForEach(totals, id: \.0) { year, total in
                StatRow(String(year), total.money)
            }
            if let highest = totals.max(by: { $0.1 < $1.1 }) {
                StatRow("Highest year", String(highest.0))
            }
        }
    }

    private var intervals: some View {
        Section("Time Between Payments") {
            BucketBars(buckets: stats.gapBuckets, color: tint)
            if let common = stats.gapBuckets.max(by: { $0.count < $1.count }) {
                StatRow("Most common", common.label)
            }
            StatRow("Median gap", "\(stats.medianGap) days")
            StatRow("Schedule", stats.schedule)
        }
    }

    private static func monthLabel(_ month: Date) -> String {
        month.formatted(.dateTime.month(.abbreviated).year())
    }
}

extension View {
    /// Sizing and dollar axis shared by the charts of amounts.
    func moneyChart() -> some View {
        chartYAxis {
            AxisMarks { value in
                AxisGridLine()
                AxisValueLabel {
                    if let amount = value.as(Double.self) { Text(amount.wholeMoney) }
                }
            }
        }
        .frame(height: 180)
        .padding(.vertical, 4)
    }
}

/// How many transactions fall in each bucket: label, bar, count.
private struct BucketBars: View {
    let buckets: [DetailStats.Bucket]
    let color: Color

    var body: some View {
        let most = max(buckets.map(\.count).max() ?? 0, 1)
        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
            ForEach(buckets) { bucket in
                GridRow {
                    Text(bucket.label)
                        .foregroundStyle(.secondary)
                    GeometryReader { space in
                        RoundedRectangle(cornerRadius: 3)
                            .fill(color)
                            .frame(width: space.size.width * CGFloat(bucket.count) / CGFloat(most))
                    }
                    .frame(height: 14)
                    Text("\(bucket.count)")
                        .monospacedDigit()
                        .gridColumnAlignment(.trailing)
                }
            }
        }
        .font(.caption)
        .padding(.vertical, 6)
    }
}
