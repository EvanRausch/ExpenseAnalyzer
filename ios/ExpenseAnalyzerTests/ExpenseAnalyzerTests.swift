//
//  ExpenseAnalyzerTests.swift
//  ExpenseAnalyzerTests
//
//  Created by Evan Rausch-Larouche on 2026-11-21.
//

import Foundation
import Testing

@testable import ExpenseAnalyzer

private func day(_ text: String) -> Date {
    let format = DateFormatter()
    format.locale = Locale(identifier: "en_US_POSIX")
    format.dateFormat = "yyyy-MM-dd"
    return format.date(from: text)!
}

private func tx(
    _ key: String, on date: String = "2026-03-02", merchant: String = "Metro", amount: Double = 10,
    category: String = "Groceries", account: String = "credit", mode: String = "in_person",
    bankDescription: String? = nil
) -> Transaction {
    Transaction(
        id: key, key: key, date: day(date), merchant: merchant, summary: "",
        bankDescription: bankDescription ?? "\(merchant) \(key)", amount: amount,
        category: category,
        account: account, isIncome: amount < 0, mode: mode, place: "", phone: "", website: "",
        notes: "")
}

struct ExportTests {
    // The shape `export_for_phone` writes on the Mac
    static let json = """
        {"exportedAt": "2026-11-21T20:22:56Z", "appliedEdits": ["done"],
         "transactions": [
          {"key": "a", "date": "2026-03-02", "merchant": "Metro", "summary": "Groceries at Metro",
           "bankDescription": "ACHAT METRO #123", "amount": 42.5, "category": "Groceries", "account": "debit",
           "isIncome": false, "mode": "in_person", "place": "Metro Plus", "phone": "514-555-0100",
           "website": "https://metro.example", "notes": ""},
          {"key": "a", "date": "2026-03-02", "merchant": "Metro", "summary": "", "bankDescription": "ACHAT METRO #123",
           "amount": 42.5, "category": "Groceries", "account": "debit", "isIncome": false, "mode": "in_person",
           "place": "", "phone": "", "website": "", "notes": ""},
          {"key": "pay", "date": "2026-03-05", "merchant": "Acme", "summary": "", "bankDescription": "PAIE ACME",
           "amount": -2000, "category": "Income", "account": "debit", "isIncome": true, "mode": "transfer",
           "place": "", "phone": "", "website": "", "notes": ""}],
         "trash": [], "categories": ["Groceries", "Income"],
         "groups": [{"name": "Food & drink", "emoji": "🍽️", "categories": ["Groceries", "Restaurants"]},
                    {"name": "Not expenses", "emoji": "💰", "categories": ["Income"]}],
         "emojis": {"Groceries": "🛒", "Food & drink": "🍽️"},
         "categoryDescriptions": {"Groceries": "Food shops"}, "merchantDescriptions": {"Groceries": {"Metro": "Main shop"}},
         "officialIncomeSources": ["Acme"], "aiAnalysis": "## Trend"}
        """

    @Test func readsTheMacExport() throws {
        let file = try ExportFile.decode(Data(Self.json.utf8))

        #expect(file.transactions.count == 3)
        #expect(file.transactions[0].date == day("2026-03-02"))
        #expect(file.transactions[0].amount == 42.5)
        #expect(file.transactions[2].isIncome)
        #expect(file.transactions[2].size == 2000)
        #expect(file.exportedAt == ISO8601DateFormatter().date(from: "2026-11-21T20:22:56Z"))
        #expect(file.merchantDescriptions["Groceries"]?["Metro"] == "Main shop")
        #expect(file.appliedEdits == ["done"])
        #expect(file.groups.map(\.name) == ["Food & drink", "Not expenses"])
        #expect(file.group(of: "Groceries") == "Food & drink")
        #expect(file.group(of: "Mystery") == "Other")
    }

    @Test func identicalRowsGetDifferentIds() throws {
        let file = try ExportFile.decode(Data(Self.json.utf8))

        #expect(Set(file.transactions.map(\.id)).count == 3)
        #expect(file.transactions[0].key == file.transactions[1].key)
    }
}

struct PendingEditTests {
    private let file = ExportFile(
        appliedEdits: ["old"],
        transactions: [
            tx("m1"), tx("m2"), tx("m3", category: "Restaurants"),
            tx("t1", merchant: "Jamie", category: "Interac Transfer", mode: "transfer"),
            tx("t2", merchant: "Jamie", category: "Interac Transfer", mode: "transfer"),
            tx("n1", merchant: "Cafe", bankDescription: "CAFE"),
            tx("n2", merchant: "Cafe", bankDescription: "CAFE"),
        ],
        trash: [tx("gone")],
        categories: ["Groceries", "Restaurants"],
        groups: [
            CategoryGroup(name: "Food & drink", emoji: "🍽️", categories: ["Groceries", "Restaurants"])
        ])

    private func category(_ key: String, in file: ExportFile) -> String? {
        file.transactions.first { $0.key == key }?.category
    }

    @Test func aCategoryChangeFollowsTheMerchantWithinTheOldCategory() {
        let result = file.applying([Edit(type: .setCategory, keys: ["m1"], value: "Snacks")])

        #expect(category("m1", in: result) == "Snacks")
        #expect(category("m2", in: result) == "Snacks")
        #expect(category("m3", in: result) == "Restaurants")
        #expect(result.categories == ["Groceries", "Restaurants", "Snacks"])
    }

    @Test func aNewCategoryJoinsItsGroupAndUnknownGroupsMeanOther() {
        let result = file.applying([
            Edit(type: .createCategory, value: "Snacks", group: "Food & drink"),
            Edit(type: .createCategory, value: "Odd", group: "No such group"),
            Edit(type: .setCategory, keys: ["m1"], value: "Snacks"),
        ])

        #expect(result.group(of: "Snacks") == "Food & drink")
        #expect(result.groups.map(\.name) == ["Food & drink", "Other"])
        #expect(result.group(of: "Odd") == "Other")
        #expect(result.categories == ["Groceries", "Odd", "Restaurants", "Snacks"])
        #expect(category("m1", in: result) == "Snacks")
    }

    @Test func aCategoryChangeOnATransferStaysOnThatTransfer() {
        let result = file.applying([Edit(type: .setCategory, keys: ["t1"], value: "Gifts")])

        #expect(category("t1", in: result) == "Gifts")
        #expect(category("t2", in: result) == "Interac Transfer")
    }

    @Test func aNoteCoversEveryRowWithTheSameBankDescription() {
        let result = file.applying([Edit(type: .setNote, keys: ["n1"], value: "With Sam")])

        #expect(result.transactions.filter { $0.notes == "With Sam" }.map(\.key) == ["n1", "n2"])
    }

    @Test func trashAndRestoreMoveRowsBetweenTheLists() {
        let trashed = file.applying([Edit(type: .trash, keys: ["m1"])])
        #expect(!trashed.transactions.contains { $0.key == "m1" })
        #expect(trashed.trash.map(\.key) == ["gone", "m1"])

        let restored = file.applying([Edit(type: .restore, keys: ["gone"])])
        #expect(restored.trash.isEmpty)
        #expect(restored.transactions.contains { $0.key == "gone" })
    }

    @Test func officialSourcesCanBeMarkedAndUnmarked() {
        let marked = file.applying([Edit(type: .markOfficial, value: "Acme")])
        #expect(marked.officialIncomeSources == ["Acme"])

        let unmarked = marked.applying([Edit(type: .unmarkOfficial, value: "Acme")])
        #expect(unmarked.officialIncomeSources.isEmpty)
    }

    @Test func editsTheMacAlreadyMadeAreNotRepeated() {
        let result = file.applying([Edit(id: "old", type: .trash, keys: ["m1"])])

        #expect(result.transactions.count == file.transactions.count)
    }
}

struct FilterTests {
    private let rows = [
        tx("a", on: "2025-12-31", account: "debit"), tx("b", on: "2026-01-01"),
        tx("c", on: "2026-01-31"),
        tx("d", on: "2026-02-01"),
    ]

    private func keys(_ filter: TransactionFilter) -> [String] {
        filter.apply(rows).map(\.key)
    }

    @Test func filtersByAccount() {
        #expect(keys(TransactionFilter(account: .debit)) == ["a"])
        #expect(keys(TransactionFilter(account: .credit)) == ["b", "c", "d"])
        #expect(keys(TransactionFilter()) == ["a", "b", "c", "d"])
    }

    @Test func filtersByYearAndMonth() {
        #expect(keys(TransactionFilter(period: .year(2026))) == ["b", "c", "d"])
        #expect(keys(TransactionFilter(period: .month(day("2026-01-15")))) == ["b", "c"])
    }

    @Test func aRangeIncludesBothEnds() {
        #expect(
            keys(TransactionFilter(period: .range(day("2026-01-01"), day("2026-01-31")))) == [
                "b", "c",
            ])
    }

    @Test func aScopeNarrowsToCategoryAndMerchant() {
        let rows = [tx("a"), tx("b", merchant: "IGA"), tx("c", category: "Rent")]

        #expect(Scope().select(from: rows).count == 3)
        #expect(Scope(category: "Groceries").select(from: rows).map(\.key) == ["a", "b"])
        #expect(
            Scope(category: "Groceries", merchant: "IGA").select(from: rows).map(\.key) == ["b"])
    }
}

struct TotalsTests {
    @Test func groupsUseAbsoluteAmountsLargestFirst() {
        let rows = [
            tx("a", amount: 10), tx("b", amount: -5), tx("c", merchant: "IGA", amount: 40),
        ]

        let groups = rows.grouped(by: \.merchant)

        #expect(groups.map(\.name) == ["IGA", "Metro"])
        #expect(groups.map(\.total) == [40, 15])
        #expect(groups.map(\.count) == [1, 2])
        #expect(rows.total == 55)
    }

    @Test func monthsAreInOrderWithTheirChange() {
        let rows = [
            tx("a", on: "2026-03-20", amount: 150), tx("b", on: "2026-01-05", amount: 100),
            tx("c", on: "2026-03-02", amount: 50), tx("d", on: "2026-04-01", amount: 100),
        ]

        let months = rows.monthly()
        let change = changes(months)

        #expect(months.map(\.total) == [100, 200, 100])
        #expect(change[months[0].month] == nil)
        #expect(change[months[1].month] == 1)
        #expect(change[months[2].month] == -0.5)
    }

    @Test func overviewMatchesTheMacFigures() {
        let rows = [
            tx("a", on: "2026-01-05", amount: 300, category: "Rent", mode: "transfer"),
            tx("b", on: "2026-01-06", amount: 60, mode: "online"),
            tx("c", on: "2026-02-06", amount: 40),
            tx("d", on: "2026-02-07", amount: 0, mode: ""),
        ]

        let stats = OverallStats(spending: rows)

        #expect(stats.totalExpense == 400)
        #expect(stats.monthly.count == 2)
        #expect(stats.averageMonthly == 200)
        #expect(stats.biggest?.key == "a")
        #expect(stats.topCategory?.name == "Rent")
        #expect(stats.topCategoryShare == 0.75)
        #expect(stats.onlineShare == 0.25)
        #expect(stats.inPersonShare == 0.25)
    }
}

struct DetailStatsTests {
    @Test func weekdaysStartOnMonday() {
        // 2 March 2026 is a Monday, 8 March a Sunday
        let stats = DetailStats([
            tx("a", on: "2026-03-02", amount: 7), tx("b", on: "2026-03-08", amount: 3),
        ])

        #expect(stats.weekdays.map(\.sum) == [7, 0, 0, 0, 0, 0, 3])
        #expect(stats.weekdays.first?.label == "Monday")
    }

    @Test func sizeBucketsIncludeTheirLowerBoundAndDropEmptyTopBuckets() {
        let stats = DetailStats([
            tx("a", amount: 24.99), tx("b", amount: 25), tx("c", amount: 99.99),
            tx("d", amount: -100),
        ])

        #expect(stats.sizes.map(\.label) == ["$0–25", "$25–50", "$50–100", "$100–200"])
        #expect(stats.sizes.map(\.count) == [1, 1, 1, 1])
    }

    @Test func medianIsTheUpperMiddleValue() {
        #expect(
            DetailStats([
                tx("a", amount: 1), tx("b", amount: 9), tx("c", amount: 5), tx("d", amount: 7),
            ]).median == 7)
        #expect(DetailStats([]).median == 0)
    }

    @Test func cumulativeTotalsRunByDay() {
        let stats = DetailStats([
            tx("b", on: "2026-03-05", amount: 20), tx("a", on: "2026-03-02", amount: 10),
            tx("c", on: "2026-03-05", amount: 5),
        ])

        #expect(stats.cumulative.map(\.value) == [10, 35])
        #expect(stats.total == 35)
    }

    @Test func monthChangesArePercentages() {
        let stats = DetailStats([
            tx("a", on: "2026-01-05", amount: 100), tx("b", on: "2026-02-05", amount: 150),
            tx("c", on: "2026-03-05", amount: 75),
        ])

        #expect(stats.monthChanges.map(\.value) == [50, -50])
    }

    @Test func yearsAreSplitByMonth() {
        let stats = DetailStats([
            tx("a", on: "2025-12-05", amount: 1), tx("b", on: "2026-01-05", amount: 2),
            tx("c", on: "2026-01-20", amount: 3),
        ])

        #expect(stats.years == [2025, 2026])
        #expect(stats.yearMonths.map(\.total) == [1, 5])
        #expect(stats.yearMonths.map(\.month) == [12, 1])
    }

    @Test func payScheduleComesFromTheUsualGap() {
        let biweekly = DetailStats([
            tx("a", on: "2026-01-01", amount: -1), tx("b", on: "2026-01-15", amount: -1),
            tx("c", on: "2026-01-29", amount: -1), tx("d", on: "2026-03-30", amount: -1),
        ])
        #expect(biweekly.gaps == [14, 14, 60])
        #expect(biweekly.medianGap == 14)
        #expect(biweekly.schedule == "bi-weekly")
        #expect(biweekly.gapBuckets.map(\.count) == [0, 0, 2, 0, 0, 1])

        let monthly = DetailStats([tx("a", on: "2026-01-01"), tx("b", on: "2026-02-01")])
        #expect(monthly.schedule == "monthly")
        #expect(
            monthly.gapBuckets.map(\.label) == [
                "< 7 days", "7–14 days", "14–21 days", "21–35 days",
            ])

        let irregular = DetailStats([tx("a", on: "2026-01-01"), tx("b", on: "2026-01-21")])
        #expect(irregular.schedule == "~20d intervals")
    }
}

@MainActor
struct DataStoreTests {
    private let server: FixtureTransport
    private let files: URL

    init() throws {
        server = try FixtureTransport(data: Data(ExportTests.json.utf8))
        files = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    private func store(token: String? = "t") -> DataStore {
        DataStore(api: API(server: Server.defaultURL, token: token, transport: server), files: files)
    }

    @Test func loadsAndSplitsSpendingFromIncome() async {
        let store = store()
        await store.reload()

        #expect(store.hasData)
        #expect(store.errorMessage == nil)
        #expect(store.spending.count == 2)
        #expect(store.income.map(\.merchant) == ["Acme"])
        #expect(store.isOfficial("Acme"))
        #expect(store.emoji(for: "Groceries") == "🛒")
        #expect(store.status?.banks == ["Demo Bank"])
    }

    @Test func aChangeShowsAtOnceAndIsSentToTheServer() async {
        let store = store()
        await store.reload()

        store.setCategory("Snacks", for: store.spending[0])
        #expect(store.spending.allSatisfy { $0.category == "Snacks" })
        await store.sendPendingEdits()

        #expect(store.pendingEdits.isEmpty)
        #expect(server.received.map(\.type) == [.setCategory])
        #expect(server.received[0].keys == ["a"])
        #expect(server.file.transactions[0].category == "Snacks")
        #expect(store.spending.allSatisfy { $0.category == "Snacks" })
    }

    @Test func aNewCategoryIsSentWithItsGroup() async {
        let store = store()
        await store.reload()

        store.createCategory("Snacks", in: "Food & drink")
        store.setCategory("Snacks", for: store.spending[0])
        await store.sendPendingEdits()

        #expect(store.group(of: "Snacks") == "Food & drink")
        #expect(server.received.map(\.type) == [.createCategory, .setCategory])
        #expect(server.received[0].group == "Food & drink")
        #expect(server.file.group(of: "Snacks") == "Food & drink")
    }

    @Test func changesMadeOfflineAreKeptAndSentLater() async throws {
        let first = store()
        await first.reload()
        server.offline = true
        first.moveToTrash([first.income[0]])
        await first.sendPendingEdits()
        #expect(first.errorMessage != nil)
        let id = try #require(first.pendingEdits.first?.id)

        // Opened again without a connection: the last data and the change are still there
        let second = store()
        #expect(second.hasData)
        #expect(second.pendingEdits.map(\.id) == [id])
        #expect(second.income.isEmpty)
        #expect(second.data.trash.map(\.key) == ["pay"])

        server.offline = false
        await second.reload()

        #expect(second.pendingEdits.isEmpty)
        #expect(second.errorMessage == nil)
        #expect(server.file.trash.map(\.key) == ["pay"])
        #expect(server.received.map(\.id) == [id])
    }

    @Test func aLostConnectionIsReportedWithoutLosingWhatIsShown() async {
        let store = store()
        await store.reload()
        server.offline = true

        await store.reload()

        #expect(store.errorMessage != nil)
        #expect(store.spending.count == 2)
    }

    @Test func signingInChecksTheTokenAndSigningOutForgetsEverything() async {
        let store = store(token: nil)
        #expect(!store.isSignedIn)

        server.signedOut = true
        #expect(await store.signIn(token: "wrong", server: Server.defaultURL) == false)
        #expect(!store.isSignedIn)
        #expect(store.errorMessage != nil)

        server.signedOut = false
        #expect(await store.signIn(token: " right\n", server: Server.defaultURL))
        #expect(store.isSignedIn)
        #expect(store.spending.count == 2)

        await store.signOut()
        #expect(!store.isSignedIn)
        #expect(!store.hasData)
        #expect(store.spending.isEmpty)
    }

    @Test func beingRefusedByTheServerGoesBackToSigningIn() async {
        let store = store()
        await store.reload()
        server.signedOut = true

        await store.reload()

        #expect(!store.isSignedIn)
        #expect(store.errorMessage != nil)
    }
}

struct ChartLayoutTests {
    @Test func treemapAreasFollowTheAmountsAndStayInside() {
        let rect = CGRect(x: 0, y: 0, width: 300, height: 200)
        let items = [("a", 50.0), ("b", 30.0), ("c", 15.0), ("d", 5.0)]
        let placed = Treemap.layout(items.map { (id: $0.0, value: $0.1) }, in: rect)

        #expect(placed.count == 4)
        for (id, value) in items {
            let cell = placed[id]!
            #expect(rect.insetBy(dx: -0.01, dy: -0.01).contains(cell))
            #expect(abs(cell.width * cell.height - value * 600) < 0.01)
        }
        // No two cells overlap
        let cells = Array(placed.values)
        for i in cells.indices {
            for j in cells.indices where j > i {
                #expect(cells[i].intersection(cells[j]).width * cells[i].intersection(cells[j]).height < 0.01)
            }
        }
    }

    @Test func bubblesNeverOverlapAndFillTheView() {
        let groups = [GroupTotal(name: "G1", total: 100, count: 3), GroupTotal(name: "G2", total: 40, count: 2)]
        let members = [
            "G1": [GroupTotal(name: "a", total: 60, count: 1), GroupTotal(name: "b", total: 30, count: 1), GroupTotal(name: "c", total: 10, count: 1)],
            "G2": [GroupTotal(name: "d", total: 25, count: 1), GroupTotal(name: "e", total: 15, count: 1)],
        ]
        let unit = Bubbles.unitLayout(groups: groups) { members[$0] ?? [] }
        let rect = CGRect(x: 0, y: 0, width: 320, height: 300)
        let laid = Bubbles.fit(unit, in: rect)

        #expect(laid.bubbles.count == 5)
        for i in laid.bubbles.indices {
            for j in laid.bubbles.indices where j > i {
                let a = laid.bubbles[i], b = laid.bubbles[j]
                #expect(hypot(a.x - b.x, a.y - b.y) >= a.r + b.r - 0.01)
            }
            let bubble = laid.bubbles[i]
            #expect(bubble.x - bubble.r >= -0.01 && bubble.x + bubble.r <= rect.width + 0.01)
            #expect(bubble.y - bubble.r >= -0.01 && bubble.y + bubble.r <= rect.height + 0.01)
        }
        // Areas keep their proportions after fitting
        let a = laid.bubbles.first { $0.name == "a" }!, d = laid.bubbles.first { $0.name == "d" }!
        #expect(abs((a.r * a.r) / (d.r * d.r) - 60.0 / 25.0) < 0.01)
        // Zooming re-fits the same packing at a larger size
        let zoomed = Bubbles.fit(unit, in: CGRect(x: 0, y: 0, width: 640, height: 600))
        #expect(abs(zoomed.bubbles[0].r - laid.bubbles[0].r * 2) < 0.01)
    }
}
