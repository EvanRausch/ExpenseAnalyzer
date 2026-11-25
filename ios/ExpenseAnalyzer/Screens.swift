import SwiftUI

/// The screens a row can lead to inside a tab.
enum Route: Hashable {
    /// The categories of a group (level 1 of the two-level list).
    case categories(String)
    /// The merchants of a category.
    case merchants(String)
    case transactions(Scope)
    case stats(Scope)
    /// A transaction, by id.
    case detail(String)
}

/// Pushes a screen from views that cannot be NavigationLinks (the treemap's tiles).
struct NavigateAction {
    var go: (Route) -> Void = { _ in }
    func callAsFunction(_ route: Route) { go(route) }
}

extension EnvironmentValues {
    @Entry var navigate = NavigateAction()
}

extension View {
    /// Where a tab's rows lead, given the transactions the tab is currently showing.
    func drillDown(into transactions: [Transaction]) -> some View {
        navigationDestination(for: Route.self) { route in
            switch route {
            case .categories(let group):
                CategoriesScreen(group: group, transactions: transactions)
            case .merchants(let category):
                MerchantsScreen(
                    category: category,
                    transactions: transactions.filter { $0.category == category })
            case .transactions(let scope):
                TransactionsScreen(scope: scope, transactions: scope.select(from: transactions))
            case .stats(let scope):
                StatsScreen(
                    title: scope.title, transactions: scope.select(from: transactions),
                    isIncome: scope.isIncome)
            case .detail(let id):
                TransactionDetailScreen(id: id)
            }
        }
    }
}

/// Toolbar link to the charts for a group of transactions.
struct StatsLink: View {
    let scope: Scope

    var body: some View {
        NavigationLink(value: Route.stats(scope)) {
            Label("Statistics", systemImage: "chart.bar.xaxis")
        }
    }
}

/// The categories of one group, largest first.
struct CategoriesScreen: View {
    @Environment(DataStore.self) private var store
    let group: String
    /// The transactions the tab is showing; only this group's are used.
    let transactions: [Transaction]

    var body: some View {
        let mine = transactions.filter { store.group(of: $0.category) == group }
        let categories = mine.grouped(by: \.category)
        let total = mine.total
        List {
            Section {
                TreemapView(
                    groups: [GroupTotal(name: group, total: total, count: mine.count)],
                    transactions: mine, height: 200)
            }
            Section("Categories") {
                NavigationLink(value: Route.transactions(scope)) {
                    GroupRow(
                        title: "All",
                        group: GroupTotal(name: "All", total: total, count: mine.count),
                        grandTotal: total)
                }
                ForEach(categories) { category in
                    NavigationLink(value: Route.merchants(category.name)) {
                        GroupRow(
                            title: "\(store.emoji(for: category.name)) \(category.name)",
                            subtitle: store.data.categoryDescriptions[category.name] ?? "",
                            group: category,
                            grandTotal: total,
                            color: Palette.color(0),
                            showsBar: true)
                    }
                }
            }
        }
        .navigationTitle("\(store.emoji(for: group)) \(group)")
        .toolbar { StatsLink(scope: scope) }
    }

    private var scope: Scope {
        Scope(
            group: group,
            members: store.data.groups.first { $0.name == group }?.categories ?? [])
    }
}

struct MerchantsScreen: View {
    @Environment(DataStore.self) private var store
    let category: String
    /// The category's transactions.
    let transactions: [Transaction]

    var body: some View {
        let total = transactions.total
        List {
            if let description = store.data.categoryDescriptions[category], !description.isEmpty {
                Section {
                    Text(description).font(.subheadline).foregroundStyle(.secondary)
                }
            }
            Section("Merchants") {
                NavigationLink(value: Route.transactions(Scope(category: category))) {
                    GroupRow(
                        title: "All",
                        group: GroupTotal(name: "All", total: total, count: transactions.count),
                        grandTotal: total)
                }
                ForEach(transactions.grouped(by: \.merchant)) { merchant in
                    NavigationLink(
                        value: Route.transactions(
                            Scope(category: category, merchant: merchant.name))
                    ) {
                        GroupRow(
                            title: merchant.name,
                            subtitle: store.data.merchantDescriptions[category]?[merchant.name]
                                ?? "",
                            group: merchant,
                            grandTotal: total)
                    }
                }
            }
        }
        .navigationTitle("\(store.emoji(for: category)) \(category)")
        .toolbar { StatsLink(scope: Scope(category: category)) }
    }
}

struct TransactionsScreen: View {
    enum Order: String, CaseIterable, Identifiable {
        case amount = "Largest first"
        case newest = "Newest first"
        case oldest = "Oldest first"
        case merchant = "By merchant"

        var id: Self { self }
    }

    @Environment(DataStore.self) private var store
    let scope: Scope
    let transactions: [Transaction]
    @State private var search = ""
    @State private var order = Order.newest

    var body: some View {
        let shown = self.shown
        List {
            Section("\(shown.count) transactions · \(shown.total.money)") {
                ForEach(shown) { transaction in
                    NavigationLink(value: Route.detail(transaction.id)) {
                        TransactionRow(
                            transaction: transaction, emoji: store.emoji(for: transaction.category))
                    }
                    .swipeActions {
                        Button("Trash", systemImage: "trash", role: .destructive) {
                            store.moveToTrash([transaction])
                        }
                    }
                }
            }
        }
        .overlay {
            if shown.isEmpty {
                if search.isEmpty {
                    ContentUnavailableView("No transactions", systemImage: "tray")
                } else {
                    ContentUnavailableView.search(text: search)
                }
            }
        }
        .searchable(text: $search, prompt: "Merchant or description")
        .navigationTitle(scope.title)
        .toolbar {
            Menu {
                Picker("Sort", selection: $order) {
                    ForEach(Order.allCases) { Text($0.rawValue).tag($0) }
                }
            } label: {
                Label("Sort", systemImage: "arrow.up.arrow.down")
            }
            StatsLink(scope: scope)
        }
    }

    private var shown: [Transaction] {
        let matching =
            search.isEmpty
            ? transactions
            : transactions.filter {
                $0.bankDescription.localizedCaseInsensitiveContains(search)
                    || $0.merchant.localizedCaseInsensitiveContains(search)
            }
        return switch order {
        case .amount: matching.sorted { $0.size > $1.size }
        case .newest: matching.sorted { $0.date > $1.date }
        case .oldest: matching.sorted { $0.date < $1.date }
        case .merchant:
            matching.sorted {
                $0.merchant.localizedCaseInsensitiveCompare($1.merchant) == .orderedAscending
            }
        }
    }
}

struct TransactionDetailScreen: View {
    @Environment(DataStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let id: String
    @State private var note = ""
    @FocusState private var editingNote: Bool
    @State private var addingCategory = false
    /// The group whose categories the picker lists; nil until the person changes it
    @State private var pickedGroup: String?

    var body: some View {
        if let transaction = store.transaction(id: id) {
            details(transaction, inTrash: store.isInTrash(transaction))
        } else {
            ContentUnavailableView(
                "This transaction is no longer available", systemImage: "questionmark.circle")
        }
    }

    private func details(_ transaction: Transaction, inTrash: Bool) -> some View {
        Form {
            Section {
                VStack(alignment: .leading, spacing: 4) {
                    Text(transaction.merchant).font(.title3.bold())
                    if !transaction.summary.isEmpty {
                        Text(transaction.summary).foregroundStyle(.secondary)
                    }
                }
                LabeledContent("Amount") { AmountText(amount: transaction.amount) }
                LabeledContent(
                    "Date", value: transaction.date.formatted(date: .long, time: .omitted))
                LabeledContent("Account", value: transaction.account.capitalized)
                if transaction.isPending {
                    LabeledContent("Status") { PendingBadge() }
                }
            } footer: {
                if transaction.isPending {
                    Text("The bank has not posted this line yet; the amount or wording may still change.")
                }
            }

            // The Mac only edits transactions that are not in the trash
            if inTrash {
                Section {
                    LabeledContent("Category", value: label(transaction.category))
                    if !transaction.notes.isEmpty {
                        LabeledContent("Notes", value: transaction.notes)
                    }
                }
            } else {
                Section {
                    let group = pickedGroup ?? store.group(of: transaction.category)
                    Picker("Group", selection: Binding(get: { group }, set: { pickedGroup = $0 })) {
                        ForEach(store.data.groups) { group in
                            Text("\(group.emoji) \(group.name)").tag(group.name)
                        }
                    }
                    Picker("Category", selection: category(of: transaction)) {
                        ForEach(categories(in: group, including: transaction.category), id: \.self) {
                            category in
                            Text(label(category)).tag(category)
                        }
                    }
                    Button("New Category…") { addingCategory = true }
                } footer: {
                    if transaction.mode != "transfer" {
                        Text(
                            "Other \(transaction.merchant) transactions in the same category change too."
                        )
                    }
                }
                Section("Notes") {
                    TextField("Add a note", text: $note, axis: .vertical)
                        .lineLimit(1...6)
                        .focused($editingNote)
                    if note != transaction.notes {
                        Button("Save Note") {
                            store.setNote(note, for: transaction)
                            editingNote = false
                        }
                    }
                }
            }

            Section("Details") {
                LabeledContent("Bank description", value: transaction.bankDescription)
                if let mode = Self.modes[transaction.mode] {
                    LabeledContent("Paid", value: mode)
                }
                if !transaction.place.isEmpty {
                    LabeledContent("Place", value: transaction.place)
                }
                if let url = phoneURL(transaction.phone) {
                    Link(destination: url) { Label(transaction.phone, systemImage: "phone") }
                }
                if let url = websiteURL(transaction.website) {
                    Link(destination: url) {
                        Label(url.host() ?? transaction.website, systemImage: "safari")
                    }
                }
            }

            Section {
                if inTrash {
                    Button("Restore", systemImage: "arrow.uturn.backward") {
                        store.restore([transaction])
                        dismiss()
                    }
                } else {
                    Button("Move to Trash", systemImage: "trash", role: .destructive) {
                        store.moveToTrash([transaction])
                        dismiss()
                    }
                }
            }
        }
        .navigationTitle("Transaction")
        .scrollDismissesKeyboard(.immediately)
        .onAppear { note = transaction.notes }
        .sheet(isPresented: $addingCategory) {
            NewCategorySheet(group: pickedGroup ?? store.group(of: transaction.category)) {
                name, group in
                store.createCategory(name, in: group)
                store.setCategory(name, for: transaction)
                pickedGroup = group
            }
        }
    }

    private static let modes = [
        "online": "Online", "in_person": "In person", "transfer": "Transfer",
    ]

    private func label(_ category: String) -> String {
        "\(store.emoji(for: category)) \(category)"
    }

    /// The categories of a group; the transaction's own first when it is in another group.
    private func categories(in group: String, including current: String) -> [String] {
        let members = store.data.groups.first { $0.name == group }?.categories ?? []
        return members.contains(current) ? members : [current] + members
    }

    private func category(of transaction: Transaction) -> Binding<String> {
        Binding(
            get: { transaction.category },
            set: { if $0 != transaction.category { store.setCategory($0, for: transaction) } })
    }

    private func phoneURL(_ phone: String) -> URL? {
        let digits = phone.filter { $0.isNumber || $0 == "+" }
        return digits.isEmpty ? nil : URL(string: "tel:\(digits)")
    }

    private func websiteURL(_ website: String) -> URL? {
        guard !website.isEmpty else { return nil }
        return URL(string: website.contains("://") ? website : "https://\(website)")
    }
}

/// Asks for the name and group of a category of the person's own.
struct NewCategorySheet: View {
    @Environment(DataStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State var group: String
    @State private var name = ""
    let add: (String, String) -> Void

    var body: some View {
        NavigationStack {
            Form {
                TextField("Name", text: $name)
                    .accessibilityIdentifier("newCategoryName")
                Picker("Group", selection: $group) {
                    ForEach(store.data.groups) { group in
                        Text("\(group.emoji) \(group.name)").tag(group.name)
                    }
                }
            }
            .navigationTitle("New Category")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") {
                        add(name.trimmingCharacters(in: .whitespaces), group)
                        dismiss()
                    }
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
        .presentationDetents([.medium])
    }
}
