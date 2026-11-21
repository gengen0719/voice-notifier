import AVFoundation
import Foundation
import Security
import SwiftUI

@main
struct VoiceNotifierApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}

struct Credentials: Codable, Equatable {
    let httpURL: URL
    let socketURL: URL
    let deviceId: String
    let token: String
}

struct Enrollment: Codable {
    let httpURL: URL
    let socketURL: URL
    let code: String
    let token: String

    init(http: String, socket: String, code: String) throws {
        httpURL = try NotifierAPI.endpoint(http, scheme: "https")
        socketURL = try NotifierAPI.endpoint(socket, scheme: "wss")
        let trimmedCode = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedCode.isEmpty else { throw NotifierError.invalidCode }
        self.code = trimmedCode
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else { throw NotifierError.keychain(status) }
        token = bytes.map { String(format: "%02x", $0) }.joined()
    }
}

enum NotifierError: LocalizedError {
    case invalidEndpoint, invalidCode, invalidResponse, status(Int), playback, keychain(OSStatus)

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint: "HTTPS と WSS の API URL を確認してください。"
        case .invalidCode: "登録コードを入力してください。"
        case .invalidResponse: "サーバーの応答を読み取れませんでした。"
        case .status(401): "端末登録の有効期限または失効状態を確認してください。"
        case .status(let code): "サーバーから HTTP \(code) が返されました。"
        case .playback: "音声を再生できませんでした。"
        case .keychain: "端末登録を Keychain に保存または読み込みできませんでした。"
        }
    }
}

enum CredentialStore {
    static let enrollment = "enrollment"

    private static func query(_ account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "voice-notifier",
            kSecAttrAccount as String: account,
        ]
    }

    static func load<T: Decodable>(_ type: T.Type, account: String = "device") throws -> T? {
        var result: CFTypeRef?
        var request = query(account)
        request[kSecReturnData as String] = true
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw NotifierError.keychain(status)
        }
        return try JSONDecoder().decode(type, from: data)
    }

    static func save<T: Encodable>(_ value: T, account: String = "device") throws {
        let data = try JSONEncoder().encode(value)
        let update = [kSecValueData as String: data]
        var status = SecItemUpdate(query(account) as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var request = query(account)
            request[kSecValueData as String] = data
            request[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            status = SecItemAdd(request as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NotifierError.keychain(status) }
    }

    static func clear(account: String = "device") throws {
        let status = SecItemDelete(query(account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NotifierError.keychain(status)
        }
    }
}

struct NotifierAPI {
    struct Device: Decodable { let deviceId: String }
    struct Audio: Decodable { let audioId: String }
    struct Page: Decodable { let sourceId: String; let items: [Audio] }
    struct Download: Decodable { let url: URL }

    let credentials: Credentials

    static func register(_ enrollment: Enrollment) async throws -> Credentials {
        var request = URLRequest(url: enrollment.httpURL.appending(path: "v1/devices"), timeoutInterval: 20)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["code": enrollment.code, "token": enrollment.token])
        let device = try JSONDecoder().decode(Device.self, from: await data(request))
        return Credentials(httpURL: enrollment.httpURL, socketURL: enrollment.socketURL, deviceId: device.deviceId, token: enrollment.token)
    }

    static func endpoint(_ value: String, scheme: String) throws -> URL {
        guard let parts = URLComponents(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme == scheme, let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              let url = parts.url else { throw NotifierError.invalidEndpoint }
        return url
    }

    private func request(_ path: String, method: String = "GET") -> URLRequest {
        let url = credentials.httpURL.appending(path: path)
        var request = URLRequest(url: url, timeoutInterval: 20)
        request.httpMethod = method
        request.setValue("Bearer " + credentials.token, forHTTPHeaderField: "Authorization")
        return request
    }

    private static func data(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: request)
        try validate(response)
        return data
    }

    static func validate(_ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse else { throw NotifierError.invalidResponse }
        guard (200..<300).contains(response.statusCode) else { throw NotifierError.status(response.statusCode) }
    }

    func page() async throws -> Page {
        try JSONDecoder().decode(Page.self, from: await Self.data(request("v1/audio")))
    }

    func download(_ id: String) async throws -> URL {
        let request = request("v1/audio/\(id)/download", method: "POST")
        let signed = try JSONDecoder().decode(Download.self, from: await Self.data(request))
        guard signed.url.scheme == "https" else { throw NotifierError.invalidResponse }
        let (file, response) = try await URLSession.shared.download(for: URLRequest(url: signed.url, timeoutInterval: 60))
        do {
            try Self.validate(response)
            return file
        } catch {
            try? FileManager.default.removeItem(at: file)
            // A storage failure must not be interpreted as a revoked device.
            throw NotifierError.playback
        }
    }

    func acknowledge(_ id: String) async throws {
        _ = try await Self.data(request("v1/audio/\(id)/played", method: "PUT"))
    }

    func socket() -> URLSessionWebSocketTask {
        var request = URLRequest(url: credentials.socketURL, timeoutInterval: 20)
        request.setValue("Bearer " + credentials.token, forHTTPHeaderField: "Authorization")
        return URLSession.shared.webSocketTask(with: request)
    }
}

final class PlaybackDelegate: NSObject, AVAudioPlayerDelegate {
    let completion: AsyncStream<Bool>.Continuation

    init(_ completion: AsyncStream<Bool>.Continuation) {
        self.completion = completion
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        completion.yield(flag)
        completion.finish()
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        completion.finish()
    }
}

@MainActor
final class NotifierModel: ObservableObject {
    @Published private(set) var credentials: Credentials?
    @Published private(set) var connection = "未接続"
    @Published private(set) var playback = "待機中"
    @Published private(set) var registering = false
    @Published private(set) var pendingEnrollment: Enrollment?
    @Published var error: String?

    init() {
        do {
            credentials = try CredentialStore.load(Credentials.self)
            if credentials == nil {
                pendingEnrollment = try CredentialStore.load(Enrollment.self, account: CredentialStore.enrollment)
            }
        } catch { self.error = error.localizedDescription }
    }

    func register(http: String, socket: String, code: String) async {
        guard !registering else { return }
        registering = true
        defer { registering = false }
        error = nil
        do {
            let attempt: Enrollment
            if let pendingEnrollment {
                attempt = pendingEnrollment
            } else {
                attempt = try Enrollment(http: http, socket: socket, code: code)
                try CredentialStore.save(attempt, account: CredentialStore.enrollment)
                pendingEnrollment = attempt
            }
            let result = try await NotifierAPI.register(attempt)
            try CredentialStore.save(result)
            credentials = result
            try clearPendingEnrollment()
        } catch NotifierError.status(let status) where [400, 403, 404].contains(status) {
            do {
                try clearPendingEnrollment()
                switch status {
                case 403: error = "登録コードが無効か、別の端末が登録されています。"
                case 404: error = "HTTP API URL に端末登録の経路が見つかりません。"
                default: error = "登録内容を確認してください。"
                }
            } catch { self.error = error.localizedDescription }
        } catch { self.error = error.localizedDescription }
    }

    private func clearPendingEnrollment() throws {
        try CredentialStore.clear(account: CredentialStore.enrollment)
        pendingEnrollment = nil
    }

    func discardPendingEnrollment() {
        guard !registering else { return }
        do {
            try clearPendingEnrollment()
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    func clearRegistration() {
        do {
            try clearPendingEnrollment()
            try CredentialStore.clear()
            if let credentials {
                UserDefaults.standard.removeObject(forKey: pendingKey(credentials))
                UserDefaults.standard.removeObject(forKey: pendingSourcesKey(credentials))
            }
            credentials = nil
        } catch { self.error = error.localizedDescription }
    }

    private func pendingKey(_ credentials: Credentials) -> String {
        "pending-played." + credentials.deviceId
    }

    private func pendingSourcesKey(_ credentials: Credentials) -> String {
        "pending-played-sources." + credentials.deviceId
    }

    func run(_ credentials: Credentials) async {
        let api = NotifierAPI(credentials: credentials)
        let (updates, wake) = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        wake.yield(())
        let realtime = Task { await self.reconnect(api, wake: wake) }
        let poll = Task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(120)) } catch { return }
                wake.yield(())
            }
        }
        defer {
            wake.finish()
            realtime.cancel()
            poll.cancel()
            connection = "未接続"
            playback = "待機中"
        }
        for await _ in updates {
            do {
                try Task.checkCancellation()
                try await drain(api)
                playback = "待機中"
            } catch is CancellationError {
                return
            } catch NotifierError.status(401) {
                self.error = NotifierError.status(401).localizedDescription
                clearRegistration()
                return
            } catch {
                if Task.isCancelled { return }
                playback = "再試行待ち"
                self.error = error.localizedDescription
                do { try await Task.sleep(for: .seconds(10)) } catch { return }
                wake.yield(())
            }
        }
    }

    private func drain(_ api: NotifierAPI) async throws {
        let defaults = UserDefaults.standard
        let legacyKey = pendingKey(api.credentials)
        let sourcesKey = pendingSourcesKey(api.credentials)
        while true {
            try Task.checkCancellation()
            let page = try await api.page()
            var pending = defaults.dictionary(forKey: sourcesKey) as? [String: String] ?? [:]
            if pending[page.sourceId] == nil,
               let legacy = defaults.string(forKey: legacyKey),
               legacy == page.items.first?.audioId {
                pending[page.sourceId] = legacy
                defaults.set(pending, forKey: sourcesKey)
                defaults.removeObject(forKey: legacyKey)
            }
            if let played = pending[page.sourceId] {
                try await api.acknowledge(played)
                pending.removeValue(forKey: page.sourceId)
                defaults.set(pending, forKey: sourcesKey)
                continue
            }
            guard !page.items.isEmpty else { return }
            for item in page.items {
                try Task.checkCancellation()
                playback = "ダウンロード中"
                let file = try await api.download(item.audioId)
                defer { try? FileManager.default.removeItem(at: file) }
                playback = "再生中"
                try await play(file)
                pending[page.sourceId] = item.audioId
                defaults.set(pending, forKey: sourcesKey)
                try await api.acknowledge(item.audioId)
                pending.removeValue(forKey: page.sourceId)
                defaults.set(pending, forKey: sourcesKey)
                error = nil
            }
        }
    }

    private func play(_ file: URL) async throws {
        try Task.checkCancellation()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback)
        try session.setActive(true)
        defer { try? session.setActive(false, options: .notifyOthersOnDeactivation) }

        let player = try AVAudioPlayer(contentsOf: file, fileTypeHint: AVFileType.mp3.rawValue)
        let (events, completion) = AsyncStream<Bool>.makeStream()
        let delegate = PlaybackDelegate(completion)
        player.delegate = delegate
        defer {
            player.stop()
            withExtendedLifetime(delegate) {}
        }
        let interruption = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: session, queue: nil
        ) { notification in
            if notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt == AVAudioSession.InterruptionType.began.rawValue {
                completion.finish()
            }
        }
        defer { NotificationCenter.default.removeObserver(interruption) }
        guard player.play() else { throw NotifierError.playback }
        for await completed in events {
            if completed { return }
        }
        try Task.checkCancellation()
        throw NotifierError.playback
    }

    private actor Heartbeat {
        private var lastPong: ContinuousClock.Instant?

        func receivedPong() { lastPong = .now }

        func receivedPong(since sent: ContinuousClock.Instant) -> Bool {
            lastPong.map { $0 >= sent } ?? false
        }
    }

    private struct SocketMessage: Decodable { let type: String; let version: Int }

    private func reconnect(_ api: NotifierAPI, wake: AsyncStream<Void>.Continuation) async {
        var delay = 1.0
        while !Task.isCancelled {
            connection = "接続中"
            let started = ContinuousClock.now
            let heartbeat = Heartbeat()
            let socket = api.socket()
            socket.resume()
            do {
                try await withTaskCancellationHandler {
                    try await withThrowingTaskGroup(of: Void.self) { group in
                        group.addTask {
                            while !Task.isCancelled {
                                let sent = ContinuousClock.now
                                try await socket.send(.string(#"{"action":"ping"}"#))
                                try await Task.sleep(until: sent.advanced(by: .seconds(20)), clock: .continuous)
                                guard await heartbeat.receivedPong(since: sent) else { return }
                                try await Task.sleep(until: sent.advanced(by: .seconds(240)), clock: .continuous)
                            }
                        }
                        group.addTask {
                            while !Task.isCancelled {
                                let message = try await socket.receive()
                                await self.process(message, heartbeat: heartbeat, wake: wake)
                            }
                        }
                        // Renew the connection before API Gateway's two-hour limit.
                        group.addTask { try await Task.sleep(for: .seconds(6900)) }
                        defer {
                            group.cancelAll()
                            socket.cancel(with: .goingAway, reason: nil)
                        }
                        _ = try await group.next()
                    }
                } onCancel: {
                    socket.cancel(with: .goingAway, reason: nil)
                }
            } catch {
                if Task.isCancelled { return }
            }
            guard !Task.isCancelled else { return }
            connection = "再接続待ち"
            if started.duration(to: .now) > .seconds(30) { delay = 1 }
            do { try await Task.sleep(for: .seconds(Double.random(in: delay / 2...delay))) }
            catch { return }
            delay = min(delay * 2, 60)
        }
    }

    private func process(
        _ frame: URLSessionWebSocketTask.Message,
        heartbeat: Heartbeat,
        wake: AsyncStream<Void>.Continuation
    ) async {
        guard !Task.isCancelled else { return }
        let data: Data
        switch frame {
        case .string(let message): data = Data(message.utf8)
        case .data(let message): data = message
        @unknown default: return
        }
        guard let message = try? JSONDecoder().decode(SocketMessage.self, from: data),
              message.version == 1 else { return }
        if message.type == "pong" {
            await heartbeat.receivedPong()
        } else if message.type != "audio.available" {
            return
        }
        guard !Task.isCancelled else { return }
        if connection != "接続済み" || message.type == "audio.available" {
            wake.yield(())
        }
        connection = "接続済み"
    }
}

struct ContentView: View {
    @Environment(\.scenePhase) private var phase
    @StateObject private var model = NotifierModel()
    @State private var http = ""
    @State private var socket = ""
    @State private var code = ""
    @State private var confirmingReset = false
    @State private var confirmingDiscard = false

    private var trimmedCode: String {
        code.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            Form {
                if model.credentials != nil {
                    Section("配信") {
                        LabeledContent("接続", value: model.connection)
                        LabeledContent("再生", value: model.playback)
                    }
                    Button("端末登録を解除", role: .destructive) { confirmingReset = true }
                } else {
                    Section("端末登録") {
                        if model.pendingEnrollment == nil {
                            TextField("HTTP API URL", text: $http)
                                .keyboardType(.URL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                            TextField("WebSocket URL", text: $socket)
                                .keyboardType(.URL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                            SecureField("登録コード", text: $code)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                        }
                        Button(model.registering ? "登録中…" : model.pendingEnrollment == nil ? "登録" : "前回の登録を再試行") {
                            Task {
                                await model.register(http: http, socket: socket, code: trimmedCode)
                                if model.credentials != nil { code = "" }
                            }
                        }
                        .disabled(model.registering || (model.pendingEnrollment == nil && (http.isEmpty || socket.isEmpty || trimmedCode.isEmpty)))
                        if let attempt = model.pendingEnrollment {
                            LabeledContent("再試行先", value: attempt.httpURL.host ?? "")
                            Button("入力をやり直す", role: .destructive) { confirmingDiscard = true }
                                .disabled(model.registering)
                        }
                    }
                }
                if let error = model.error {
                    Text(error).foregroundStyle(.red)
                }
            }
            .navigationTitle("Voice Notifier")
            .confirmationDialog("保留中の登録を破棄しますか？", isPresented: $confirmingDiscard) {
                Button("破棄して入力し直す", role: .destructive) { model.discardPendingEnrollment() }
            } message: {
                Text("サーバーで登録済みの場合、再登録には管理者による端末の失効が必要です。")
            }
            .confirmationDialog("このiPhoneの登録情報を消去しますか？", isPresented: $confirmingReset) {
                Button("消去", role: .destructive) { model.clearRegistration() }
            } message: {
                Text("再登録するときは、管理者にサーバー側の端末失効と新しい登録コードの発行を依頼してください。")
            }
        }
        .task(id: phase == .active ? model.credentials : nil) {
            if phase == .active, let credentials = model.credentials {
                await model.run(credentials)
            }
        }
    }
}
