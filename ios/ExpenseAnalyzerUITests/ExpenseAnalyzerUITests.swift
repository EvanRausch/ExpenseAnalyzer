//
//  ExpenseAnalyzerUITests.swift
//  ExpenseAnalyzerUITests
//
//  Created by Evan Rausch-Larouche on 2026-11-21.
//

import XCTest

/// Walks through every tab against a folder of made-up data, with no server (see FixtureTransport).
///
/// Needs `TEST_RUNNER_DATA_FOLDER` (a folder written by `python ios/make_demo_data.py <folder>`) in
/// the environment of `xcodebuild test`; `TEST_RUNNER_SCREENSHOT_DIR` additionally saves a picture
/// of each screen.
final class ExpenseAnalyzerUITests: XCTestCase {
    private var app: XCUIApplication!
    private var dataFolder = ""
    private var shots = 0

    override func setUpWithError() throws {
        continueAfterFailure = false
        dataFolder = try XCTUnwrap(
            ProcessInfo.processInfo.environment["DATA_FOLDER"],
            "Set TEST_RUNNER_DATA_FOLDER to run this test")
        app = XCUIApplication()
        app.launchArguments = ["-dataFolder", dataFolder]
        app.launch()
    }

    @MainActor
    func testBrowsingAndEditing() throws {
        // Summary: filters, hiding a group, drilling down group -> category -> merchant
        XCTAssert(app.navigationBars["Summary"].waitForExistence(timeout: 20))
        snap("summary")
        app.buttons["All dates"].tap()
        app.buttons["Year"].tap()
        app.buttons["2026"].tap()
        XCTAssert(app.buttons["2026"].waitForExistence(timeout: 5))
        app.buttons["All"].tap()
        app.buttons["Credit"].tap()
        snap("summary-filtered")

        row("Transportation").swipeLeft()
        app.buttons["Hide"].tap()
        XCTAssertFalse(row("Transportation", timeout: 2).exists)
        snap("summary-hidden")
        app.navigationBars.buttons["Shown Categories"].tap()
        app.buttons["Fuel & gas stations"].tap()
        XCTAssert(row("Transportation").exists)

        row("Food & drink").tap()
        XCTAssert(row("Supermarkets & grocery stores").exists)
        snap("categories")
        row("Supermarkets & grocery stores").tap()
        XCTAssert(row("Maple Market").exists)
        snap("merchants")
        row("Maple Market").tap()
        XCTAssert(app.navigationBars.buttons["Sort"].waitForExistence(timeout: 5))
        snap("transactions")

        // Charts for the merchant
        app.navigationBars.buttons["Statistics"].tap()
        XCTAssert(app.staticTexts["Monthly average"].waitForExistence(timeout: 5))
        snapScrolling("stats", pages: 4)
        app.navigationBars.buttons.element(boundBy: 0).tap()

        // Editing a transaction: the merchant's rows follow it to the new category
        app.collectionViews.buttons.element(boundBy: 0).tap()
        XCTAssert(app.navigationBars["Transaction"].waitForExistence(timeout: 5))
        snap("detail")
        // The group first, then one of its categories
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Group'")).firstMatch.tap()
        app.buttons.matching(NSPredicate(format: "label ENDSWITH 'Gifts & donations'")).firstMatch
            .tap()
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Category'")).firstMatch.tap()
        app.buttons.matching(NSPredicate(format: "label ENDSWITH 'gift cards'")).firstMatch.tap()
        let note = app.descendants(matching: .any)
            .matching(NSPredicate(format: "placeholderValue == 'Add a note'")).firstMatch
        note.tap()
        note.typeText("Birthday dinner")
        app.buttons["Save Note"].tap()
        snap("detail-edited")
        app.swipeUp()
        app.buttons["Move to Trash"].tap()
        XCTAssert(app.staticTexts["No transactions"].waitForExistence(timeout: 5))

        // Transactions: categories under their groups, sorting and search
        app.tabBars.buttons["Transactions"].tap()
        snap("transactions-tab")
        row("All transactions").tap()
        app.navigationBars.buttons["Sort"].tap()
        app.buttons["Newest first"].tap()
        // The search field sits above the list until it is pulled down
        app.swipeDown()
        app.searchFields.firstMatch.tap()
        app.searchFields.firstMatch.typeText("luna")
        XCTAssert(row("Luna Pizzeria").exists)
        XCTAssertFalse(row("Maple Market", timeout: 1).exists)
        snap("search")
        // "Cancel" up to iOS 26, a close button from iOS 27
        app.buttons.matching(NSPredicate(format: "label IN {'Cancel', 'Close', 'Dismiss'}"))
            .firstMatch.tap()

        app.tabBars.buttons["Statistics"].tap()
        XCTAssert(app.staticTexts["Total expenses"].waitForExistence(timeout: 5))
        snapScrolling("statistics-tab", pages: 7)

        // Income: official sources and the per-payment charts
        app.tabBars.buttons["Income"].tap()
        row("Canada Revenue Agency").swipeLeft()
        app.buttons["Official"].tap()
        XCTAssert(row("⭐ Canada Revenue Agency").exists)
        snap("income")
        row("Acme Corp").tap()
        app.navigationBars.buttons["Statistics"].tap()
        XCTAssert(app.staticTexts["Payments"].waitForExistence(timeout: 5))
        snapScrolling("income-stats", pages: 4)

        // Trash: the transaction trashed above is there, and one can come back
        app.tabBars.buttons["Trash"].tap()
        XCTAssert(row("Maple Market").exists)
        snap("trash")
        row("Gadget Depot").swipeLeft()
        app.buttons["Restore"].tap()
        XCTAssertFalse(row("Gadget Depot", timeout: 2).exists)

        app.buttons["syncMenu"].tap()
        let bank = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == 'Demo Bank'")).firstMatch
        XCTAssert(bank.waitForExistence(timeout: 5))
        snap("sync-menu")
        app.buttons["Refresh"].tap()

        // What the server was sent (the stand-in server lists it in the data folder)
        let file = try Data(
            contentsOf: URL(fileURLWithPath: dataFolder).appendingPathComponent("phone_edits.json"))
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: file) as? [String: [[String: Any]]])
        XCTAssertEqual(
            json["edits"]?.compactMap { $0["type"] as? String },
            ["setCategory", "setNote", "trash", "markOfficial", "restore"])
    }

    /// A list row showing `text`.
    private func row(_ text: String, timeout: TimeInterval = 5) -> XCUIElement {
        let row = app.collectionViews.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", text)
        ).firstMatch
        _ = row.waitForExistence(timeout: timeout)
        return row
    }

    private func snap(_ name: String) {
        guard let folder = ProcessInfo.processInfo.environment["SCREENSHOT_DIR"] else { return }
        shots += 1
        let file = URL(fileURLWithPath: folder).appendingPathComponent(
            String(format: "%02d-%@.png", shots, name))
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: file)
    }

    private func snapScrolling(_ name: String, pages: Int) {
        for page in 1...pages {
            snap("\(name)-\(page)")
            app.swipeUp()
        }
    }
}
