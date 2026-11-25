import Foundation
import Observation

/// The phone's copy of the server's data, with the changes still on their way already shown.
///
/// The last answer from the server and the changes not yet sent are kept in `files`, so the app
/// opens with data when offline and no change is lost.
@Observable
@MainActor
final class DataStore {
    private(set) var data = ExportFile()
    private(set) var spending: [Transaction] = []
    private(set) var income: [Transaction] = []
    /// Changes the server has not taken yet (no connection, or still being sent)
    private(set) var pendingEdits: [Edit] = []
    /// False until the server's data has been seen once (on this run or a previous one)
    private(set) var hasData = false
    private(set) var errorMessage: String?
    private(set) var isSignedIn: Bool
    private(set) var status: SyncStatus?
    /// A bank sync we asked for is being waited on
    private(set) var isSyncing = false

    private var exported = ExportFile()
    private var api: API
    private let files: URL
    private var sending = false

    private static let tokenKey = "token"
    private static let serverKey = "serverURL"
    private static let cacheFile = "export.json"
    private static let editsFile = "pending_edits.json"

    /// The app's store: the keychain and UserDefaults say which server and which token.
    ///
    /// Launch arguments for the simulator and tests: `-serverURL <url>` and `-token <token>`
    /// sign in without the screen; `-dataFolder <path>` answers from that folder's
    /// `transactions.json` instead of any server.
    convenience init() {
        let defaults = UserDefaults.standard
        if let path = defaults.string(forKey: "dataFolder"),
            let fixture = try? FixtureTransport(folder: URL(fileURLWithPath: path, isDirectory: true))
        {
            let scratch = FileManager.default.temporaryDirectory.appendingPathComponent(
                UUID().uuidString, isDirectory: true)
            self.init(
                api: API(server: Server.defaultURL, token: "demo", transport: fixture),
                files: scratch)
            return
        }
        let server = defaults.string(forKey: Self.serverKey).flatMap(URL.init) ?? Server.defaultURL
        let token = defaults.string(forKey: Self.tokenKey) ?? Keychain.read(Self.tokenKey)
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        self.init(
            api: API(server: server, token: token, transport: URLSession.shared),
            files: support.appendingPathComponent("ExpenseAnalyzer", isDirectory: true))
    }

    init(api: API, files: URL) {
        self.api = api
        self.files = files
        isSignedIn = api.token != nil
        try? FileManager.default.createDirectory(at: files, withIntermediateDirectories: true)
        if isSignedIn { restore() }
    }

    var server: URL { api.server }

    // MARK: Signing in

    /// Check the token with the server and keep it. False, with `errorMessage`, if it was refused.
    func signIn(token: String, server: URL) async -> Bool {
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        let candidate = API(server: server, token: token, transport: api.transport)
        do {
            status = try await candidate.get("/v1/status", as: SyncStatus.self)
        } catch API.Failure.signedOut {
            errorMessage = "The server does not know this token. Check it and try again."
            return false
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
        api = candidate
        Keychain.write(token, for: Self.tokenKey)
        UserDefaults.standard.set(server.absoluteString, forKey: Self.serverKey)
        isSignedIn = true
        errorMessage = nil
        await reload()
        return true
    }

    /// Forget the token on this phone only: it stays valid, so the same token signs in again
    /// (people share one phone between accounts).
    func signOut() {
        forget(saying: nil)
    }

    private func forget(saying message: String?) {
        Keychain.write(nil, for: Self.tokenKey)
        api.token = nil
        isSignedIn = false
        hasData = false
        exported = ExportFile()
        pendingEdits = []
        status = nil
        rebuild()
        for name in [Self.cacheFile, Self.editsFile] {
            try? FileManager.default.removeItem(at: files.appendingPathComponent(name))
        }
        errorMessage = message
    }

    // MARK: Talking to the server

    /// Fetch the server's data and send any change it has not taken yet.
    func reload() async {
        guard isSignedIn else { return }
        do {
            async let export = api.get("/v1/export")
            async let fresh = api.get("/v1/status", as: SyncStatus.self)
            let (answer, status) = try await (export, fresh)
            try accept(answer)
            self.status = status
            errorMessage = nil
        } catch {
            report(error)
        }
        await sendPendingEdits()
    }

    /// Ask the server to fetch new transactions from the bank, wait for it, then reload.
    func refresh() async {
        guard isSignedIn else { return }
        isSyncing = true
        defer { isSyncing = false }
        do {
            let started = try await api.post("/v1/sync", as: [String: Bool].self)["started"] ?? false
            // Up to a minute; a longer sync shows up on the next refresh
            for _ in 0..<20 where started {
                try await Task.sleep(for: .seconds(3))
                status = try await api.get("/v1/status", as: SyncStatus.self)
                if status?.syncing == false { break }
            }
        } catch {
            report(error)
        }
        await reload()
    }

    /// The page where the person logs in to their bank, or nil (with `errorMessage`).
    func startBankLink() async -> URL? {
        do {
            let answer = try await api.post("/v1/bank/link", as: [String: String].self)
            return answer["url"].flatMap(URL.init)
        } catch {
            report(error)
            return nil
        }
    }

    /// Once the bank page is closed: true if a bank was connected (its first sync has started).
    func finishBankLink() async -> Bool {
        do {
            status = try await api.post("/v1/bank/link/complete", as: SyncStatus.self)
            errorMessage = nil
            return true
        } catch {
            report(error)
            return false
        }
    }

    /// Send the changes waiting, oldest first; the answer is the server's data with them in.
    func sendPendingEdits() async {
        guard isSignedIn, !sending else { return }
        sending = true
        defer { sending = false }
        // Changes made while a batch is on its way go in the next one
        while !pendingEdits.isEmpty {
            let batch = pendingEdits
            do {
                let body = try JSONEncoder().encode(EditFile(edits: batch))
                let answer = try await api.post("/v1/edits", body: body)
                let sent = Set(batch.map(\.id))
                pendingEdits.removeAll { sent.contains($0.id) }
                saveEdits()
                try accept(answer)
                errorMessage = nil
            } catch {
                report(error)
                return
            }
        }
    }

    private func report(_ error: Error) {
        if case API.Failure.signedOut = error {
            forget(saying: "The server no longer accepts this sign-in. Sign in again.")
        } else {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: Changes

    func setCategory(_ category: String, for transaction: Transaction) {
        add(Edit(type: .setCategory, keys: [transaction.key], value: category))
    }

    func setNote(_ note: String, for transaction: Transaction) {
        add(Edit(type: .setNote, keys: [transaction.key], value: note))
    }

    func moveToTrash(_ transactions: [Transaction]) {
        add(Edit(type: .trash, keys: Array(Set(transactions.map(\.key)))))
    }

    func restore(_ transactions: [Transaction]) {
        add(Edit(type: .restore, keys: Array(Set(transactions.map(\.key)))))
    }

    func setOfficial(_ official: Bool, source: String) {
        add(Edit(type: official ? .markOfficial : .unmarkOfficial, value: source))
    }

    /// Add a category of the person's own under a group.
    func createCategory(_ name: String, in group: String) {
        add(Edit(type: .createCategory, value: name, group: group))
    }

    private func add(_ edit: Edit) {
        pendingEdits.append(edit)
        rebuild()
        saveEdits()
        Task { await sendPendingEdits() }
    }

    // MARK: Lookups

    func transaction(id: String) -> Transaction? {
        data.transactions.first { $0.id == id } ?? data.trash.first { $0.id == id }
    }

    func isInTrash(_ transaction: Transaction) -> Bool {
        data.trash.contains { $0.id == transaction.id }
    }

    func emoji(for category: String) -> String {
        data.emojis[category] ?? "🏷️"
    }

    /// The group (level 1) a category belongs to.
    func group(of category: String) -> String {
        data.group(of: category)
    }

    func isOfficial(_ source: String) -> Bool {
        data.officialIncomeSources.contains(source)
    }

    // MARK: Local copy

    private func accept(_ answer: Data) throws {
        exported = try ExportFile.decode(answer)
        hasData = true
        rebuild()
        try? answer.write(to: files.appendingPathComponent(Self.cacheFile), options: .atomic)
    }

    private func restore() {
        if let cached = try? Data(contentsOf: files.appendingPathComponent(Self.cacheFile)),
            let file = try? ExportFile.decode(cached)
        {
            exported = file
            hasData = true
        }
        if let saved = try? Data(contentsOf: files.appendingPathComponent(Self.editsFile)),
            let edits = try? JSONDecoder().decode(EditFile.self, from: saved)
        {
            pendingEdits = edits.edits
        }
        rebuild()
    }

    private func rebuild() {
        data = exported.applying(pendingEdits)
        spending = data.transactions.filter { !$0.isIncome }
        income = data.transactions.filter(\.isIncome)
    }

    private func saveEdits() {
        do {
            try JSONEncoder().encode(EditFile(edits: pendingEdits))
                .write(to: files.appendingPathComponent(Self.editsFile), options: .atomic)
        } catch {
            errorMessage = "Could not keep your change: \(error.localizedDescription)"
        }
    }
}
