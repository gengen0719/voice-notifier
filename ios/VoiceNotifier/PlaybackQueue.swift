import Foundation

struct AudioPage: Decodable {
    struct Audio: Decodable { let audioId: String }
    let sourceId: String
    let items: [Audio]
}

@MainActor
struct PlaybackQueue {
    let defaults: UserDefaults
    let legacyKey: String
    let sourcesKey: String

    func drain(
        page: () async throws -> AudioPage,
        playAudio: (String) async throws -> Bool,
        acknowledge: (String) async throws -> Void,
        didPlay: () -> Void = {}
    ) async throws {
        while true {
            try Task.checkCancellation()
            let page = try await page()
            var pending = defaults.dictionary(forKey: sourcesKey) as? [String: String] ?? [:]
            if pending[page.sourceId] == nil,
               let legacy = defaults.string(forKey: legacyKey),
               legacy == page.items.first?.audioId {
                pending[page.sourceId] = legacy
                defaults.set(pending, forKey: sourcesKey)
                defaults.removeObject(forKey: legacyKey)
            }
            if let played = pending[page.sourceId] {
                try await acknowledge(played)
                pending.removeValue(forKey: page.sourceId)
                defaults.set(pending, forKey: sourcesKey)
                continue
            }
            guard !page.items.isEmpty else { return }
            for item in page.items {
                try Task.checkCancellation()
                guard try await playAudio(item.audioId) else { continue }
                pending[page.sourceId] = item.audioId
                defaults.set(pending, forKey: sourcesKey)
                try await acknowledge(item.audioId)
                pending.removeValue(forKey: page.sourceId)
                defaults.set(pending, forKey: sourcesKey)
                didPlay()
            }
        }
    }
}
