import Foundation

// The figures here follow the Mac app: every total is a sum of absolute amounts.

enum AccountFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case credit
    case debit

    var id: Self { self }
}

struct TransactionFilter: Equatable {
    enum Period: Hashable {
        case all
        case year(Int)
        /// Any day inside the month.
        case month(Date)
        case range(Date, Date)
    }

    var account = AccountFilter.all
    var period = Period.all

    func apply(_ transactions: [Transaction], calendar: Calendar = .current) -> [Transaction] {
        transactions.filter { transaction in
            if account != .all && transaction.account != account.rawValue { return false }
            switch period {
            case .all:
                return true
            case .year(let year):
                return calendar.component(.year, from: transaction.date) == year
            case .month(let month):
                return calendar.isDate(transaction.date, equalTo: month, toGranularity: .month)
            case .range(let from, let to):
                return transaction.date >= calendar.startOfDay(for: from)
                    && transaction.date <= calendar.startOfDay(for: to)
            }
        }
    }

    var periodLabel: String {
        switch period {
        case .all: "All dates"
        case .year(let year): String(year)
        case .month(let month): month.formatted(.dateTime.month(.abbreviated).year())
        case .range(let from, let to):
            "\(from.formatted(date: .abbreviated, time: .omitted)) – \(to.formatted(date: .abbreviated, time: .omitted))"
        }
    }
}

/// Which transactions a pushed screen shows, out of the ones its tab has filtered.
struct Scope: Hashable {
    var category: String?
    var merchant: String?
    var isIncome = false

    func select(from transactions: [Transaction]) -> [Transaction] {
        transactions.filter {
            (category == nil || $0.category == category)
                && (merchant == nil || $0.merchant == merchant)
        }
    }

    var title: String { merchant ?? category ?? (isIncome ? "All Income" : "All Transactions") }
}

struct GroupTotal: Identifiable, Hashable {
    let name: String
    var total = 0.0
    var count = 0

    var id: String { name }
}

struct MonthTotal: Identifiable, Hashable {
    let month: Date
    var total = 0.0

    var id: Date { month }
    var label: String { month.formatted(.dateTime.month(.abbreviated).year()) }
}

extension Sequence where Element == Transaction {
    var total: Double { reduce(0) { $0 + $1.size } }

    /// Totals per name, largest first.
    func grouped(by name: (Transaction) -> String) -> [GroupTotal] {
        var groups: [String: GroupTotal] = [:]
        for transaction in self {
            let key = name(transaction)
            groups[key, default: GroupTotal(name: key)].total += transaction.size
            groups[key]!.count += 1
        }
        return groups.values.sorted { ($0.total, $1.name) > ($1.total, $0.name) }
    }

    /// Totals per calendar month, oldest first.
    func monthly(calendar: Calendar = .current) -> [MonthTotal] {
        var months: [Date: Double] = [:]
        for transaction in self {
            let month =
                calendar.dateInterval(of: .month, for: transaction.date)?.start ?? transaction.date
            months[month, default: 0] += transaction.size
        }
        return months.map { MonthTotal(month: $0.key, total: $0.value) }.sorted {
            $0.month < $1.month
        }
    }

    var largest: Transaction? { self.max { $0.size < $1.size } }
}

/// The Statistics tab's "All-Time Overview".
struct OverallStats {
    let totalExpense: Double
    let monthly: [MonthTotal]
    let averageMonthly: Double
    let biggest: Transaction?
    let topCategory: GroupTotal?
    let onlineShare: Double
    let inPersonShare: Double

    init(spending: [Transaction]) {
        totalExpense = spending.total
        monthly = spending.monthly()
        averageMonthly = monthly.isEmpty ? 0 : totalExpense / Double(monthly.count)
        biggest = spending.largest
        topCategory = spending.grouped(by: \.category).first
        let count = Double(Swift.max(spending.count, 1))
        onlineShare = Double(spending.filter { $0.mode == "online" }.count) / count
        inPersonShare = Double(spending.filter { $0.mode == "in_person" }.count) / count
    }

    var topCategoryShare: Double {
        guard let topCategory, totalExpense > 0 else { return 0 }
        return topCategory.total / totalExpense
    }
}

/// Month-over-month change, as shown beside each month: nil for the first month or after an empty one.
func changes(_ months: [MonthTotal]) -> [Date: Double] {
    var result: [Date: Double] = [:]
    for (previous, current) in zip(months, months.dropFirst()) where previous.total > 0 {
        result[current.month] = (current.total - previous.total) / previous.total
    }
    return result
}

/// The figures behind the detailed statistics screen (the Mac app's "View statistics" window).
struct DetailStats {
    struct Bucket: Identifiable, Hashable {
        let label: String
        var count = 0
        var sum = 0.0

        var id: String { label }
    }

    struct Point: Identifiable, Hashable {
        let date: Date
        let value: Double

        var id: Date { date }
    }

    struct YearMonth: Identifiable, Hashable {
        let year: Int
        let month: Int
        let total: Double

        var id: Int { year * 100 + month }
    }

    /// Oldest first.
    let transactions: [Transaction]
    let total: Double
    let monthly: [MonthTotal]
    /// Monday first.
    let weekdays: [Bucket]
    let sizes: [Bucket]
    let median: Double
    let cumulative: [Point]
    /// Percent change from the month before, for every month after the first.
    let monthChanges: [Point]
    let yearMonths: [YearMonth]
    let gaps: [Int]
    let gapBuckets: [Bucket]

    static let weekdayNames = [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    ]
    private static let sizeLimits: [(String, Double)] = [
        ("$0–25", 25), ("$25–50", 50), ("$50–100", 100), ("$100–200", 200), ("$200–500", 500),
        ("$500+", .infinity),
    ]
    private static let gapLimits: [(String, Double)] = [
        ("< 7 days", 7), ("7–14 days", 14), ("14–21 days", 21), ("21–35 days", 35),
        ("35–60 days", 60), ("60+ days", .infinity),
    ]

    init(_ unsorted: [Transaction], calendar: Calendar = .current) {
        transactions = unsorted.sorted { $0.date < $1.date }
        total = transactions.total
        monthly = transactions.monthly(calendar: calendar)

        var days = Self.weekdayNames.map { Bucket(label: $0) }
        for transaction in transactions {
            // Calendar counts Sunday as 1; the chart starts on Monday
            let index = (calendar.component(.weekday, from: transaction.date) + 5) % 7
            days[index].count += 1
            days[index].sum += transaction.size
        }
        weekdays = days

        let amounts = transactions.map(\.size)
        sizes = Self.buckets(amounts, limits: Self.sizeLimits)
        median = amounts.isEmpty ? 0 : amounts.sorted()[amounts.count / 2]

        var running = 0.0
        var points: [Date: Double] = [:]
        for transaction in transactions {
            running += transaction.size
            points[transaction.date] = running
        }
        cumulative = points.map { Point(date: $0.key, value: $0.value) }.sorted {
            $0.date < $1.date
        }

        monthChanges = zip(monthly, monthly.dropFirst()).map { previous, current in
            Point(
                date: current.month,
                value: previous.total > 0
                    ? (current.total - previous.total) / previous.total * 100 : 0)
        }

        var byYearMonth: [Int: Double] = [:]
        for transaction in transactions {
            let parts = calendar.dateComponents([.year, .month], from: transaction.date)
            byYearMonth[(parts.year ?? 0) * 100 + (parts.month ?? 0), default: 0] +=
                transaction.size
        }
        yearMonths = byYearMonth.map {
            YearMonth(year: $0.key / 100, month: $0.key % 100, total: $0.value)
        }
        .sorted { $0.id < $1.id }

        gaps = zip(transactions, transactions.dropFirst()).map { earlier, later in
            calendar.dateComponents([.day], from: earlier.date, to: later.date).day ?? 0
        }
        gapBuckets = Self.buckets(gaps.map(Double.init), limits: Self.gapLimits)
    }

    var years: [Int] { Array(Set(yearMonths.map(\.year))).sorted() }

    var medianGap: Int { gaps.isEmpty ? 0 : gaps.sorted()[gaps.count / 2] }

    /// How often payments arrive, judged from the usual gap between them.
    var schedule: String {
        switch medianGap {
        case 27...32: "monthly"
        case 13...16: "bi-weekly"
        case 6...8: "weekly"
        default: "~\(medianGap)d intervals"
        }
    }

    /// Counts per bucket (each value goes in the first bucket whose limit it is under), dropping empty buckets at the top end.
    private static func buckets(_ values: [Double], limits: [(String, Double)]) -> [Bucket] {
        var buckets = limits.map { Bucket(label: $0.0) }
        for value in values {
            let index = limits.firstIndex { value < $0.1 } ?? limits.count - 1
            buckets[index].count += 1
            buckets[index].sum += value
        }
        while let last = buckets.last, last.count == 0 { buckets.removeLast() }
        return buckets
    }
}
