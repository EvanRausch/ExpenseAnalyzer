import Foundation
import Observation

/// Reads the Mac app's export from the shared iCloud folder and writes changes back to it.
@Observable
@MainActor
final class DataStore {
    /// The Mac's data with the changes still waiting for the Mac already shown.
    private(set) var data = ExportFile()
    private(set) var spending: [Transaction] = []
    private(set) var income: [Transaction] = []
    private(set) var pendingEdits: [Edit] = []
    private(set) var folder: URL?
    /// False until the Mac's export has been read once.
    private(set) var hasData = false
    private(set) var errorMessage: String?

    private var exported = ExportFile()
    private static let bookmarkKey = "dataFolderBookmark"

    init() {
        // `-dataFolder <path>` on the command line skips the folder picker (tests, simulator)
        if let path = UserDefaults.standard.string(forKey: "dataFolder") {
            folder = URL(fileURLWithPath: path, isDirectory: true)
        } else if let bookmark = UserDefaults.standard.data(forKey: Self.bookmarkKey) {
            var isStale = false
            folder = try? URL(resolvingBookmarkData: bookmark, bookmarkDataIsStale: &isStale)
        }
    }

    init(folder: URL) {
        self.folder = folder
    }

    func useFolder(_ url: URL) async {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            UserDefaults.standard.set(try url.bookmarkData(), forKey: Self.bookmarkKey)
        } catch {
            errorMessage = "Could not remember that folder: \(error.localizedDescription)"
        }
        folder = url
        await reload()
    }

    func reload() async {
        guard let folder else { return }
        do {
            let (file, saved) = try await Task.detached { try Self.load(from: folder) }.value
            exported = file
            hasData = true
            // Keep changes made while the files were being read
            let known = Set(pendingEdits.map(\.id))
            pendingEdits += saved.filter { !known.contains($0.id) }
            let acknowledged = Set(file.appliedEdits)
            pendingEdits.removeAll { acknowledged.contains($0.id) }
            if pendingEdits != saved { saveEdits() }
            rebuild()
            errorMessage = nil
        } catch {
            errorMessage = "Could not read the data from your Mac: \(error.localizedDescription)"
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

    private func add(_ edit: Edit) {
        pendingEdits.append(edit)
        rebuild()
        saveEdits()
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

    func isOfficial(_ source: String) -> Bool {
        data.officialIncomeSources.contains(source)
    }

    // MARK: Files

    private func rebuild() {
        data = exported.applying(pendingEdits)
        spending = data.transactions.filter { !$0.isIncome }
        income = data.transactions.filter(\.isIncome)
    }

    private func saveEdits() {
        guard let folder else { return }
        do {
            let json = try JSONEncoder().encode(EditFile(edits: pendingEdits))
            try Self.write(json, to: folder.appendingPathComponent(EditFile.fileName), in: folder)
        } catch {
            errorMessage = "Could not save your change: \(error.localizedDescription)"
        }
    }

    nonisolated private static func load(from folder: URL) throws -> (ExportFile, [Edit]) {
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        let export = try read(folder.appendingPathComponent(ExportFile.fileName))
        let edits = try? read(folder.appendingPathComponent(EditFile.fileName))
        return (
            try ExportFile.decode(export),
            (edits.flatMap { try? JSONDecoder().decode(EditFile.self, from: $0) })?.edits ?? []
        )
    }

    nonisolated private static func read(_ url: URL) throws -> Data {
        // Ask iCloud for the latest copy; the coordinated read then waits for it
        try? FileManager.default.startDownloadingUbiquitousItem(at: url)
        var coordinationError: NSError?
        var result: Result<Data, Error>?
        NSFileCoordinator().coordinate(readingItemAt: url, error: &coordinationError) { readable in
            result = Result { try Data(contentsOf: readable) }
        }
        if let coordinationError { throw coordinationError }
        guard let result else { throw CocoaError(.fileReadUnknown) }
        return try result.get()
    }

    nonisolated private static func write(_ data: Data, to url: URL, in folder: URL) throws {
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        var coordinationError: NSError?
        var writeError: Error?
        NSFileCoordinator().coordinate(
            writingItemAt: url, options: .forReplacing, error: &coordinationError
        ) { writable in
            do { try data.write(to: writable, options: .atomic) } catch { writeError = error }
        }
        if let error = coordinationError as Error? ?? writeError { throw error }
    }
}
