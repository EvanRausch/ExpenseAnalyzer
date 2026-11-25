//
//  ContentView.swift
//  ExpenseAnalyzer
//
//  Created by Evan Rausch-Larouche on 2026-11-21.
//

import SwiftUI

struct ContentView: View {
    @Environment(DataStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if !store.isSignedIn {
                SignInScreen()
            } else if store.hasData {
                tabs
            } else if store.errorMessage != nil {
                trouble
            } else {
                ProgressView("Loading…")
            }
        }
        // Pick up what the server has while the app was in the background
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

    private var trouble: some View {
        ContentUnavailableView {
            Label("Could Not Load", systemImage: "wifi.exclamationmark")
        } description: {
            Text(store.errorMessage ?? "")
        } actions: {
            Button("Try Again") {
                Task { await store.reload() }
            }
            .buttonStyle(.borderedProminent)
            Button("Sign Out") { store.signOut() }
        }
    }
}

/// Asks for the sign-in token printed by `deploy/cloud.sh import-mac` (or `server.cli add-user`).
struct SignInScreen: View {
    @Environment(DataStore.self) private var store
    @State private var token = ""
    @State private var server = ""
    @State private var showingServer = false
    @State private var working = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Sign-in token", text: $token)
                        .textContentType(.password)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .accessibilityIdentifier("token")
                } header: {
                    Text("Token")
                } footer: {
                    Text(
                        "The token was shown once when your data was moved to the server. Paste it here; it stays in this phone's keychain."
                    )
                }
                if let error = store.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.red)
                    }
                }
                Section {
                    DisclosureGroup("Server", isExpanded: $showingServer) {
                        TextField("https://…", text: $server)
                            .keyboardType(.URL)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                    }
                }
            }
            .navigationTitle("Sign In")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    if working {
                        ProgressView()
                    } else {
                        Button("Sign In") { signIn() }
                            .disabled(token.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
            .onAppear { server = store.server.absoluteString }
        }
    }

    private func signIn() {
        guard let url = URL(string: server.trimmingCharacters(in: .whitespaces)),
            url.scheme != nil
        else { return }
        working = true
        Task {
            _ = await store.signIn(token: token, server: url)
            working = false
        }
    }
}

#Preview {
    ContentView()
        .environment(DataStore())
}
