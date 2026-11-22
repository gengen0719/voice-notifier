import Foundation
import XCTest
@testable import PlaybackQueue

final class PlaybackQueueTests: XCTestCase {
    private enum ScenarioError: Error { case offline, playback }

    @MainActor
    private func withQueue(_ scenario: (PlaybackQueue) async throws -> Void) async throws {
        let suite = "PlaybackQueueTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        try await scenario(PlaybackQueue(defaults: defaults, legacyKey: "legacy", sourcesKey: "pending"))
    }

    private func page(_ ids: [String], source: String = "source") -> AudioPage {
        AudioPage(sourceId: source, items: ids.map { AudioPage.Audio(audioId: $0) })
    }

    @MainActor
    func testGivenExpiredAudioBeforePlayableItems_WhenDrained_ThenPlaysAndAcknowledgesInOrder() async throws {
        try await withQueue { queue in
            // Given
            var pages = [page(["expired", "first", "second"]), page([])]
            var events: [String] = []

            // When
            try await queue.drain(
                page: { pages.removeFirst() },
                playAudio: { id in
                    events.append("request:" + id)
                    return id != "expired"
                },
                acknowledge: { events.append("ack:" + $0) }
            )

            // Then
            XCTAssertEqual(events, ["request:expired", "request:first", "ack:first", "request:second", "ack:second"])
        }
    }

    @MainActor
    func testGivenLostAcknowledgement_WhenDrainedAgain_ThenRetriesBeforeNextAudioWithoutReplaying() async throws {
        try await withQueue { queue in
            // Given
            var events: [String] = []
            do {
                try await queue.drain(
                    page: { self.page(["first", "second"]) },
                    playAudio: { events.append("play:" + $0); return true },
                    acknowledge: { events.append("ack:" + $0); throw ScenarioError.offline }
                )
                XCTFail("The acknowledgement error must be propagated")
            } catch ScenarioError.offline {}
            XCTAssertEqual(queue.defaults.dictionary(forKey: queue.sourcesKey) as? [String: String], ["source": "first"])

            // When
            var pages = [page(["first", "second"]), page(["second"]), page([])]
            try await queue.drain(
                page: { pages.removeFirst() },
                playAudio: { events.append("play:" + $0); return true },
                acknowledge: { events.append("ack:" + $0) }
            )

            // Then
            XCTAssertEqual(events, ["play:first", "ack:first", "ack:first", "play:second", "ack:second"])
            XCTAssertEqual(queue.defaults.dictionary(forKey: queue.sourcesKey) as? [String: String], [:])
        }
    }

    @MainActor
    func testGivenPlaybackFailure_WhenDrained_ThenLeavesAudioUnacknowledgedAndStopsTheQueue() async throws {
        try await withQueue { queue in
            // Given
            var played: [String] = []
            var acknowledged: [String] = []

            // When
            do {
                try await queue.drain(
                    page: { self.page(["broken", "next"]) },
                    playAudio: { played.append($0); throw ScenarioError.playback },
                    acknowledge: { acknowledged.append($0) }
                )
                XCTFail("The playback error must be propagated")
            } catch ScenarioError.playback {}

            // Then
            XCTAssertEqual(played, ["broken"])
            XCTAssertTrue(acknowledged.isEmpty)
            XCTAssertNil(queue.defaults.object(forKey: queue.sourcesKey))
        }
    }

    @MainActor
    func testGivenPendingAcknowledgementForOtherSource_WhenCurrentSourceDrains_ThenKeepsItForLater() async throws {
        try await withQueue { queue in
            // Given
            queue.defaults.set(["other": "old"], forKey: queue.sourcesKey)
            var pages = [page(["new"]), page([])]
            var events: [String] = []

            // When
            try await queue.drain(
                page: { pages.removeFirst() },
                playAudio: { events.append("play:" + $0); return true },
                acknowledge: { events.append("ack:" + $0) }
            )

            // Then
            XCTAssertEqual(events, ["play:new", "ack:new"])
            XCTAssertEqual(queue.defaults.dictionary(forKey: queue.sourcesKey) as? [String: String], ["other": "old"])
        }
    }
}
