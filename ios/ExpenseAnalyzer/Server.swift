import Foundation
import Security

/// Where the phone's data lives: the server deployed with deploy/cloud.sh.
enum Server {
    static let defaultURL = URL(
        string: "https://expenseanalyzer-1049746016384.northamerica-northeast1.run.app")!
}

/// Sends one HTTP request. URLSession in the app; a stand-in in tests and the simulator.
protocol Transport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

extension URLSession: Transport {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }
}

/// What `GET /v1/status` answers.
struct SyncStatus: Codable, Equatable {
    var banks: [String] = []
    var syncing = false
    var lastSync: String?
    var error = ""

    var lastSyncDate: Date? {
        lastSync.flatMap { ISO8601DateFormatter().date(from: $0) }
    }
}

/// The server's API, as seen by one signed-in device.
struct API: Sendable {
    var server: URL
    var token: String?
    var transport: any Transport

    enum Failure: LocalizedError, Equatable {
        /// The server no longer accepts the token
        case signedOut
        /// The server could not be reached
        case offline(String)
        /// The server answered with an error
        case server(String)

        var errorDescription: String? {
            switch self {
            case .signedOut: "Sign in again"
            case .offline(let why): "Could not reach the server: \(why)"
            case .server(let why): why
            }
        }
    }

    func get(_ path: String) async throws -> Data {
        try await send("GET", path, body: nil)
    }

    func post(_ path: String, body: Data? = nil) async throws -> Data {
        try await send("POST", path, body: body)
    }

    func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        try JSONDecoder().decode(type, from: try await get(path))
    }

    func post<T: Decodable>(_ path: String, body: Data? = nil, as type: T.Type) async throws -> T {
        try JSONDecoder().decode(type, from: try await post(path, body: body))
    }

    private func send(_ method: String, _ path: String, body: Data?) async throws -> Data {
        var request = URLRequest(url: server.appending(path: path))
        request.httpMethod = method
        request.timeoutInterval = 30
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch {
            throw Failure.offline(error.localizedDescription)
        }
        switch response.statusCode {
        case 200..<300: return data
        case 401: throw Failure.signedOut
        default:
            let detail = try? JSONDecoder().decode([String: String].self, from: data)["detail"]
            throw Failure.server(detail ?? "The server answered with error \(response.statusCode)")
        }
    }
}

/// The sign-in token, kept in the keychain so it survives reinstalling the app.
enum Keychain {
    private static let service = Bundle.main.bundleIdentifier ?? "ExpenseAnalyzer"

    static func read(_ key: String) -> String? {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: key,
            kSecReturnData: true, kSecMatchLimit: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
            let data = item as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String?, for key: String) {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: key,
        ]
        SecItemDelete(query as CFDictionary)
        guard let value else { return }
        let item = query.merging([
            kSecValueData: Data(value.utf8),
            kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]) { $1 }
        SecItemAdd(item as CFDictionary, nil)
    }
}

/// Stands in for the server with a `transactions.json` file: the simulator (`-dataFolder`), the
/// UI tests and the unit tests. Changes are applied to the file's data in memory and, when the
/// data came from a folder, also listed in `phone_edits.json` there.
final class FixtureTransport: Transport, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var file: ExportFile
    private let folder: URL?
    var status = SyncStatus(banks: ["Demo Bank"], lastSync: "2026-11-21T20:22:56Z")
    /// Answer every request as if there were no connection
    var offline = false
    /// Answer every request as if the token were no longer valid
    var signedOut = false
    private(set) var received: [Edit] = []

    init(data: Data, folder: URL? = nil) throws {
        file = try ExportFile.decode(data)
        self.folder = folder
    }

    convenience init(folder: URL) throws {
        try self.init(
            data: try Data(contentsOf: folder.appendingPathComponent(ExportFile.fileName)),
            folder: folder)
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try lock.withLock { try answer(request) }
    }

    private func answer(_ request: URLRequest) throws -> (Data, HTTPURLResponse) {
        if offline { throw URLError(.notConnectedToInternet) }
        if signedOut { return reply(request, 401, #"{"detail": "Sign in again"}"#) }
        switch (request.httpMethod ?? "GET", request.url?.path() ?? "") {
        case ("GET", "/v1/export"):
            return reply(request, 200, try encode(file))
        case ("POST", "/v1/edits"):
            let edits = try JSONDecoder().decode(EditFile.self, from: request.httpBody ?? Data())
                .edits
            file = file.applying(edits)
            file.appliedEdits += edits.map(\.id)
            received += edits
            if let folder {
                try JSONEncoder().encode(EditFile(edits: received))
                    .write(to: folder.appendingPathComponent(EditFile.fileName))
            }
            return reply(request, 200, try encode(file))
        case ("GET", "/v1/status"):
            return reply(request, 200, try JSONEncoder().encode(status))
        case ("POST", "/v1/sync"):
            return reply(request, 200, #"{"started": false}"#)
        case ("POST", "/v1/auth/logout"):
            return reply(request, 200, "{}")
        default:
            return reply(request, 404, #"{"detail": "Not found"}"#)
        }
    }

    private func encode(_ file: ExportFile) throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return try encoder.encode(file)
    }

    private func reply(_ request: URLRequest, _ status: Int, _ body: String) -> (
        Data, HTTPURLResponse
    ) {
        reply(request, status, Data(body.utf8))
    }

    private func reply(_ request: URLRequest, _ status: Int, _ body: Data) -> (
        Data, HTTPURLResponse
    ) {
        (
            body,
            HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        )
    }
}
