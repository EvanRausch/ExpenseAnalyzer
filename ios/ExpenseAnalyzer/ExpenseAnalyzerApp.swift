//
//  ExpenseAnalyzerApp.swift
//  ExpenseAnalyzer
//
//  Created by Evan Rausch-Larouche on 2026-11-21.
//

import SwiftUI

@main
struct ExpenseAnalyzerApp: App {
    @State private var store = DataStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(store)
        }
    }
}
