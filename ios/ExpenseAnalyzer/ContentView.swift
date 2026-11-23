//
//  ContentView.swift
//  ExpenseAnalyzer
//
//  Created by Evan Rausch-Larouche on 2026-11-21.
//

import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @Environment(DataStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @State private var choosingFolder = false

    var body: some View {
        Group {
            if store.hasData {
                tabs
            } else if store.folder == nil || store.errorMessage != nil {
                setup
            } else {
                ProgressView("Loading…")
            }
        }
        // Pick up whatever the Mac exported while the app was in the background
        .task(id: scenePhase) {
            if scenePhase == .active { await store.reload() }
        }
    }

    private var tabs: some View {
        TabView {
            SummaryTab()
                .tabItem { Label("Summary", systemImage: "chart.pie") }
            TransactionsTab()
                .tabItem { Label("Transactions", systemImage: "list.bullet") }
            StatisticsTab()
                .tabItem { Label("Statistics", systemImage: "chart.bar") }
            IncomeTab()
                .tabItem { Label("Income", systemImage: "arrow.down.circle") }
            TrashTab()
                .tabItem { Label("Trash", systemImage: "trash") }
        }
    }

    private var setup: some View {
        ContentUnavailableView {
            Label("Connect to Your Mac", systemImage: "icloud.and.arrow.down")
        } description: {
            Text(
                store.errorMessage
                    ?? "ExpenseAnalyzer on your Mac saves your transactions to iCloud Drive. Choose its ExpenseAnalyzer folder to see them here."
            )
        } actions: {
            Button("Choose Folder…") { choosingFolder = true }
                .buttonStyle(.borderedProminent)
            if store.folder != nil {
                Button("Try Again") {
                    Task { await store.reload() }
                }
            }
        }
        .fileImporter(isPresented: $choosingFolder, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result {
                Task { await store.useFolder(url) }
            }
        }
    }
}

#Preview {
    ContentView()
        .environment(DataStore())
}
