import Charts
import SafariServices
import SwiftUI

struct SummaryTab: View {
    @Environment(DataStore.self) private var store
    @State private var filter = TransactionFilter()
    @State private var path: [Route] = []
    /// Categories left out of the summary, one per line.
    @AppStorage("hiddenCategories") private var hiddenText = ""
    /// Which picture of the spending sits above the list
    @AppStorage("summaryChart") private var chart = "map"

    var body: some View {
        let hidden = Set(hiddenText.split(separator: "\n").map(String.init))
        let filtered = filter.apply(store.spending)
        let shown = filtered.filter { !hidden.contains($0.category) }
        // Level 1 of the two-level list; tapping one shows its categories
        let groups = shown.grouped { store.group(of: $0.category) }
        let total = shown.total
        NavigationStack(path: $path) {
            List {
                Section {
                    FilterBar(filter: $filter, transactions: store.spending)
                }
                if groups.isEmpty {
                    ContentUnavailableView("No spending", systemImage: "chart.pie")
                } else {
                    Section {
                        Picker("Chart", selection: $chart) {
                            Text("Map").tag("map")
                            Text("Bubbles").tag("bubbles")
                        }
                        .pickerStyle(.segmented)
                        .listRowSeparator(.hidden)
                        if chart == "bubbles" {
                            BubbleChartView(groups: groups, transactions: shown)
                        } else {
                            TreemapView(groups: groups, transactions: shown)
                        }
                    }
                    Section {
                        ForEach(Array(groups.enumerated()), id: \.element.id) { index, group in
                            NavigationLink(value: Route.categories(group.name)) {
                                GroupRow(
                                    title: "\(store.emoji(for: group.name)) \(group.name)",
                                    group: group,
                                    grandTotal: total,
                                    color: Palette.color(index),
                                    showsBar: true)
                            }
                            .swipeActions {
                                Button("Hide", systemImage: "eye.slash") {
                                    // Hiding a group hides the categories it holds right now
                                    let members = shown.filter {
                                        store.group(of: $0.category) == group.name
                                    }
                                    setHidden(hidden.union(members.map(\.category)))
                                }
                            }
                        }
                    } footer: {
                        if !hidden.isEmpty {
                            Text(
                                "\(hidden.count) hidden: \(hidden.sorted().joined(separator: ", "))"
                            )
                        }
                    }
                }
            }
            .navigationTitle("Summary")
            .toolbar {
                Menu {
                    let present = Set(filtered.map(\.category)).union(hidden)
                    ForEach(store.data.groups) { group in
                        let members = group.categories.filter(present.contains)
                        if !members.isEmpty {
                            Section("\(group.emoji) \(group.name)") {
                                ForEach(members, id: \.self) { category in
                                    Toggle(
                                        category,
                                        isOn: Binding(
                                            get: { !hidden.contains(category) },
                                            set: {
                                                setHidden(
                                                    $0
                                                        ? hidden.subtracting([category])
                                                        : hidden.union([category]))
                                            }
                                        ))
                                }
                            }
                        }
                    }
                } label: {
                    Label("Shown Categories", systemImage: hidden.isEmpty ? "eye" : "eye.slash")
                }
                SyncMenu()
            }
            .refreshable { await store.refresh() }
            .drillDown(into: shown)
        }
        .environment(\.navigate, NavigateAction { path.append($0) })
    }

    private func setHidden(_ categories: Set<String>) {
        hiddenText = categories.sorted().joined(separator: "\n")
    }
}

struct TransactionsTab: View {
    @Environment(DataStore.self) private var store
    @State private var filter = TransactionFilter()
    @State private var path: [Route] = []

    var body: some View {
        let filtered = filter.apply(store.spending)
        let total = filtered.total
        NavigationStack(path: $path) {
            List {
                Section {
                    FilterBar(filter: $filter, transactions: store.spending)
                }
                Section {
                    NavigationLink(value: Route.transactions(Scope())) {
                        GroupRow(
                            title: "All transactions",
                            group: GroupTotal(name: "All", total: total, count: filtered.count),
                            grandTotal: total)
                    }
                }
                ForEach(filtered.grouped { store.group(of: $0.category) }) { group in
                    let mine = filtered.filter { store.group(of: $0.category) == group.name }
                    Section {
                        ForEach(mine.grouped(by: \.category)) { category in
                            NavigationLink(value: Route.merchants(category.name)) {
                                GroupRow(
                                    title: "\(store.emoji(for: category.name)) \(category.name)",
                                    subtitle: store.data.categoryDescriptions[category.name] ?? "",
                                    group: category,
                                    grandTotal: total)
                            }
                        }
                    } header: {
                        NavigationLink(value: Route.categories(group.name)) {
                            HStack {
                                Text("\(store.emoji(for: group.name)) \(group.name)")
                                Spacer()
                                Text(group.total.money).monospacedDigit()
                            }
                        }
                    }
                }
            }
            .navigationTitle("Transactions")
            .toolbar { SyncMenu() }
            .refreshable { await store.refresh() }
            .drillDown(into: filtered)
        }
        .environment(\.navigate, NavigateAction { path.append($0) })
    }
}

struct StatisticsTab: View {
    @Environment(DataStore.self) private var store
    @State private var from: Date?
    @State private var to: Date?
    @State private var category: String?

    var body: some View {
        let spending = store.spending
        let overall = OverallStats(spending: spending)
        NavigationStack {
            List {
                if spending.isEmpty {
                    ContentUnavailableView("No spending", systemImage: "chart.bar")
                } else {
                    overview(overall)
                    trend(overall.monthly)
                    period(spending)
                    breakdown(spending)
                }
                Section("AI Spending Analysis") {
                    if store.data.aiAnalysis.isEmpty {
                        Text("No analysis yet. It is written after the next bank sync.")
                            .foregroundStyle(.secondary)
                    } else {
                        MarkdownText(text: store.data.aiAnalysis)
                    }
                }
            }
            .navigationTitle("Statistics")
            .toolbar { SyncMenu() }
            .refreshable { await store.refresh() }
            .drillDown(into: spending)
        }
    }

    private func overview(_ overall: OverallStats) -> some View {
        Section("All-Time Overview") {
            StatRow("Total expenses", overall.totalExpense.money)
            StatRow("Months covered", "\(overall.monthly.count)")
            StatRow("Average monthly spend", overall.averageMonthly.money)
            if let biggest = overall.biggest {
                StatRow("Biggest expense", "\(biggest.size.money) · \(biggest.merchant)")
            }
            if let top = overall.topCategory {
                StatRow("Top category", "\(top.name) · \(overall.topCategoryShare.percent)")
            }
            StatRow(
                "Online / in person",
                "\(overall.onlineShare.percent) / \(overall.inPersonShare.percent)")
            NavigationLink("Detailed Statistics", value: Route.stats(Scope()))
        }
    }

    private func trend(_ months: [MonthTotal]) -> some View {
        let change = changes(months)
        return Section("Monthly Spending Trend") {
            Chart(months) { month in
                BarMark(
                    x: .value("Month", month.month, unit: .month), y: .value("Total", month.total)
                )
                .foregroundStyle(Color.expense)
            }
            .moneyChart()
            ForEach(months.reversed()) { month in
                HStack {
                    Text(month.label)
                    Spacer()
                    if let change = change[month.month] {
                        // Spending more than the month before is the bad direction
                        Text("\(change > 0 ? "▲" : "▼") \(abs(change).percent)")
                            .font(.caption)
                            .foregroundStyle(change > 0 ? Color.red : Color.green)
                    }
                    Text(month.total.money).monospacedDigit().frame(
                        minWidth: 96, alignment: .trailing)
                }
            }
        }
    }

    private func period(_ spending: [Transaction]) -> some View {
        let dates = spending.map(\.date)
        let start = from ?? dates.min() ?? .now
        let end = to ?? dates.max() ?? .now
        let selected = TransactionFilter(period: .range(start, end)).apply(spending)
        return Section("Selected Period") {
            DatePicker(
                "From", selection: Binding(get: { start }, set: { from = $0 }),
                displayedComponents: .date)
            DatePicker(
                "To", selection: Binding(get: { end }, set: { to = $0 }), displayedComponents: .date
            )
            StatRow("Total expenses", selected.total.money)
            StatRow("Transactions", "\(selected.count)")
            StatRow(
                "Average per transaction",
                selected.isEmpty ? "–" : (selected.total / Double(selected.count)).money)
            if let biggest = selected.largest {
                StatRow("Biggest expense", "\(biggest.size.money) · \(biggest.merchant)")
            }
        }
    }

    @ViewBuilder private func breakdown(_ spending: [Transaction]) -> some View {
        let categories = spending.grouped(by: \.category).map(\.name)
        let chosen =
            category.flatMap { categories.contains($0) ? $0 : nil } ?? categories.first ?? ""
        let rows = spending.filter { $0.category == chosen }
        let months = rows.monthly()
        Section("Category Breakdown") {
            Picker("Category", selection: Binding(get: { chosen }, set: { category = $0 })) {
                ForEach(store.data.groups) { group in
                    let members = group.categories.filter(categories.contains)
                    if !members.isEmpty {
                        Section("\(group.emoji) \(group.name)") {
                            ForEach(members, id: \.self) {
                                Text("\(store.emoji(for: $0)) \($0)").tag($0)
                            }
                        }
                    }
                }
            }
            StatRow("Transactions", "\(rows.count)")
            StatRow("Total", rows.total.money)
            StatRow(
                "Average per transaction",
                rows.isEmpty ? "–" : (rows.total / Double(rows.count)).money)
            StatRow(
                "Average per active month",
                months.isEmpty ? "–" : (rows.total / Double(months.count)).money)
            StatRow("Largest", (rows.map(\.size).max() ?? 0).money)
            StatRow("Smallest", (rows.map(\.size).min() ?? 0).money)
            NavigationLink("Detailed Statistics", value: Route.stats(Scope(category: chosen)))
        }
        Section("Top Merchants in \(chosen)") {
            ForEach(rows.grouped(by: \.merchant).prefix(10)) { merchant in
                NavigationLink(
                    value: Route.transactions(Scope(category: chosen, merchant: merchant.name))
                ) {
                    GroupRow(title: merchant.name, group: merchant, grandTotal: rows.total)
                }
            }
        }
        Section("\(chosen) by Month") {
            Chart(months) { month in
                BarMark(
                    x: .value("Month", month.month, unit: .month), y: .value("Total", month.total)
                )
                .foregroundStyle(Color.expense)
            }
            .moneyChart()
        }
    }
}

struct IncomeTab: View {
    @Environment(DataStore.self) private var store
    @State private var filter = TransactionFilter()

    var body: some View {
        let filtered = filter.apply(store.income)
        let total = filtered.total
        NavigationStack {
            List {
                Section {
                    FilterBar(filter: $filter, transactions: store.income)
                }
                Section {
                    NavigationLink(value: Route.transactions(Scope(isIncome: true))) {
                        GroupRow(
                            title: "All",
                            group: GroupTotal(name: "All", total: total, count: filtered.count),
                            grandTotal: total)
                    }
                    ForEach(filtered.grouped(by: \.merchant)) { source in
                        let official = store.isOfficial(source.name)
                        NavigationLink(
                            value: Route.transactions(Scope(merchant: source.name, isIncome: true))
                        ) {
                            GroupRow(
                                title: official ? "⭐ \(source.name)" : source.name,
                                group: source,
                                grandTotal: total)
                        }
                        .swipeActions {
                            Button(
                                official ? "Unmark" : "Official",
                                systemImage: official ? "star.slash" : "star"
                            ) {
                                store.setOfficial(!official, source: source.name)
                            }
                            .tint(.yellow)
                        }
                    }
                } header: {
                    Text("Sources")
                } footer: {
                    Text("Swipe a source to mark it as an official income source.")
                }
            }
            .navigationTitle("Income & Inflows")
            .toolbar { SyncMenu() }
            .refreshable { await store.refresh() }
            .drillDown(into: filtered)
        }
    }
}

struct TrashTab: View {
    @Environment(DataStore.self) private var store

    var body: some View {
        let trash = store.data.trash.sorted { $0.date > $1.date }
        NavigationStack {
            List(trash) { transaction in
                NavigationLink(value: Route.detail(transaction.id)) {
                    TransactionRow(
                        transaction: transaction, emoji: store.emoji(for: transaction.category))
                }
                .swipeActions {
                    Button("Restore", systemImage: "arrow.uturn.backward") {
                        store.restore([transaction])
                    }
                    .tint(.green)
                }
            }
            .overlay {
                if trash.isEmpty {
                    ContentUnavailableView(
                        "Trash is empty", systemImage: "trash",
                        description: Text("Swipe a transaction to move it here."))
                }
            }
            .navigationTitle("Trash")
            .toolbar { SyncMenu() }
            .refreshable { await store.refresh() }
            .drillDown(into: trash)
        }
    }
}

/// Shows how fresh the data is, what is still on its way, and the bank connection.
struct SyncMenu: View {
    @Environment(DataStore.self) private var store
    @State private var bankPage: BankPage?
    @State private var signingOut = false

    private struct BankPage: Identifiable {
        let url: URL
        var id: String { url.absoluteString }
    }

    var body: some View {
        Menu {
            Section {
                if let status = store.status {
                    if status.syncing || store.isSyncing {
                        Text("Checking the bank…")
                    } else if let date = status.lastSyncDate {
                        Text("Bank checked \(date.formatted(.relative(presentation: .named)))")
                    } else if status.banks.isEmpty {
                        Text("No bank connected")
                    }
                    ForEach(status.banks, id: \.self) { bank in
                        Label(bank, systemImage: "building.columns")
                    }
                    if !status.error.isEmpty {
                        Text(status.error)
                    }
                }
                if !store.pendingEdits.isEmpty {
                    Text("\(store.pendingEdits.count) changes waiting to be sent")
                }
                if let error = store.errorMessage {
                    Text(error)
                }
            }
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await store.refresh() }
            }
            Button("Connect a Bank…", systemImage: "building.columns") {
                Task { bankPage = await store.startBankLink().map(BankPage.init) }
            }
            Button("Sign Out", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) {
                signingOut = true
            }
        } label: {
            Label("Sync", systemImage: icon)
        }
        .accessibilityIdentifier("syncMenu")
        .sheet(item: $bankPage) { page in
            SafariView(url: page.url)
                .ignoresSafeArea()
                // Closing the bank's page is when the server can tell whether it went through
                .onDisappear { Task { _ = await store.finishBankLink() } }
        }
        .confirmationDialog(
            "Sign out of this phone?", isPresented: $signingOut, titleVisibility: .visible
        ) {
            Button("Sign Out", role: .destructive) {
                Task { await store.signOut() }
            }
        } message: {
            Text("Your data stays on the server; signing in again brings it back.")
        }
    }

    private var icon: String {
        if store.errorMessage != nil { return "exclamationmark.icloud" }
        if store.isSyncing || store.status?.syncing == true {
            return "arrow.triangle.2.circlepath.icloud"
        }
        return store.pendingEdits.isEmpty ? "checkmark.icloud" : "icloud.and.arrow.up"
    }
}

/// The bank's login page, shown inside the app.
struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        SFSafariViewController(url: url)
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}

/// Headings, bullets and inline styles: enough for the Mac's AI analysis.
struct MarkdownText: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(text.split(separator: "\n").enumerated()), id: \.offset) { _, line in
                let line = line.trimmingCharacters(in: .whitespaces)
                if line.hasPrefix("#") {
                    Text(inline(String(line.drop { $0 == "#" || $0 == " " })))
                        .font(.headline)
                        .padding(.top, 4)
                } else if line.hasPrefix("- ") || line.hasPrefix("* ") {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•")
                        Text(inline(String(line.dropFirst(2))))
                    }
                } else {
                    Text(inline(line))
                }
            }
        }
        .font(.subheadline)
    }

    private func inline(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text)) ?? AttributedString(text)
    }
}
