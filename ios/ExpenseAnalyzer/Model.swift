import Foundation

/// One transaction as the server sends it. `amount` is positive for money going out.
struct Transaction: Identifiable, Hashable, Codable {
    var id = ""
    /// Opaque identity the server uses to find the same row again.
    let key: String
    let date: Date
    let merchant: String
    let summary: String
    let bankDescription: String
    let amount: Double
    var category: String
    let account: String
    let isIncome: Bool
    let mode: String
    let place: String
    let phone: String
    let website: String
    var notes: String

    var size: Double { abs(amount) }

    private enum CodingKeys: String, CodingKey {
        case key, date, merchant, summary, bankDescription, amount, category, account
        case isIncome, mode, place, phone, website, notes
    }
}

/// Everything the server answers to `GET /v1/export` (the Mac app writes the same to `transactions.json`).
struct ExportFile: Codable {
    var exportedAt = Date.distantPast
    var appliedEdits: [String] = []
    var transactions: [Transaction] = []
    var trash: [Transaction] = []
    var categories: [String] = []
    var emojis: [String: String] = [:]
    var categoryDescriptions: [String: String] = [:]
    var merchantDescriptions: [String: [String: String]] = [:]
    var officialIncomeSources: [String] = []
    var aiAnalysis = ""

    static let fileName = "transactions.json"

    static func decode(_ data: Data) throws -> ExportFile {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            if let date = dayFormat.date(from: text) ?? ISO8601DateFormatter().date(from: text) {
                return date
            }
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Unrecognised date \(text)")
            )
        }
        var file = try decoder.decode(ExportFile.self, from: data)
        // Identical rows share a key, so ids also count occurrences
        var seen: [String: Int] = [:]
        func number(_ transaction: inout Transaction) {
            seen[transaction.key, default: 0] += 1
            transaction.id = "\(transaction.key)#\(seen[transaction.key]!)"
        }
        for index in file.transactions.indices { number(&file.transactions[index]) }
        for index in file.trash.indices { number(&file.trash[index]) }
        return file
    }

    private static let dayFormat: DateFormatter = {
        let format = DateFormatter()
        format.calendar = Calendar(identifier: .gregorian)
        format.locale = Locale(identifier: "en_US_POSIX")
        format.dateFormat = "yyyy-MM-dd"
        return format
    }()
}

/// A change made on the phone, sent to the server as soon as it can be reached.
struct Edit: Codable, Hashable {
    enum Kind: String, Codable {
        case setCategory, setNote, trash, restore, markOfficial, unmarkOfficial
    }

    var id = UUID().uuidString
    let type: Kind
    var keys: [String] = []
    var value = ""
}

/// What the phone sends to `POST /v1/edits`, and keeps on disk until the server has taken it.
struct EditFile: Codable {
    var edits: [Edit] = []

    static let fileName = "phone_edits.json"
}

extension ExportFile {
    /// The data as it will look once the server has applied `edits`.
    func applying(_ edits: [Edit]) -> ExportFile {
        var file = self
        for edit in edits where !appliedEdits.contains(edit.id) {
            let keys = Set(edit.keys)
            switch edit.type {
            case .setCategory:
                for chosen in file.transactions.filter({ keys.contains($0.key) }) {
                    // The server gives the same change to the merchant's other rows in the old
                    // category, except for transfers, which can each be for something different
                    for index in file.transactions.indices {
                        let other = file.transactions[index]
                        let similar =
                            chosen.mode != "transfer" && other.merchant == chosen.merchant
                            && other.category == chosen.category
                        if other.key == chosen.key || similar {
                            file.transactions[index].category = edit.value
                        }
                    }
                }
                if !file.categories.contains(edit.value) {
                    file.categories = (file.categories + [edit.value]).sorted()
                }
            case .setNote:
                // Notes belong to the bank description, as on the server
                let descriptions = Set(
                    file.transactions.filter { keys.contains($0.key) }.map(\.bankDescription))
                for index in file.transactions.indices
                where descriptions.contains(file.transactions[index].bankDescription) {
                    file.transactions[index].notes = edit.value
                }
            case .trash:
                file.trash += file.transactions.filter { keys.contains($0.key) }
                file.transactions.removeAll { keys.contains($0.key) }
            case .restore:
                file.transactions += file.trash.filter { keys.contains($0.key) }
                file.trash.removeAll { keys.contains($0.key) }
            case .markOfficial:
                if !file.officialIncomeSources.contains(edit.value) {
                    file.officialIncomeSources.append(edit.value)
                }
            case .unmarkOfficial:
                file.officialIncomeSources.removeAll { $0 == edit.value }
            }
        }
        return file
    }
}
