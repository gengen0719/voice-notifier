import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import client


def page(*audio_ids):
    return {"sourceId": "source", "items": [{"audioId": audio_id} for audio_id in audio_ids]}


class PlaybackBehaviorTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.state = {"credentials": {"endpoint": "https://api.test", "token": "token"}}
        self.events = []

    def test_given_expired_audio_when_draining_then_plays_remaining_items_in_order(self):
        # Given
        expired = HTTPError("https://api.test", 410, "Gone", None, None)
        first = {"url": "https://storage.test/first.mp3"}
        second = {"url": "https://storage.test/second.mp3"}
        replies = Mock(side_effect=[page("expired", "first", "second"), expired, first, None,
                                   second, None, page()])

        def request(endpoint, path, method="GET", token=None):
            self.events.append((method, path))
            return replies()

        # When
        with patch.object(client, "request", side_effect=request), \
             patch.object(client, "play", side_effect=lambda url: self.events.append(("play", url))):
            client.drain(self.path, self.state)

        # Then
        self.assertEqual(self.events, [
            ("GET", "/v1/audio"),
            ("POST", "/v1/audio/expired/download"),
            ("POST", "/v1/audio/first/download"),
            ("play", first["url"]),
            ("PUT", "/v1/audio/first/played"),
            ("POST", "/v1/audio/second/download"),
            ("play", second["url"]),
            ("PUT", "/v1/audio/second/played"),
            ("GET", "/v1/audio"),
        ])

    def test_given_lost_acknowledgement_when_restarted_then_retries_without_replaying(self):
        # Given
        first = {"url": "https://storage.test/first.mp3"}
        second = {"url": "https://storage.test/second.mp3"}
        offline = client.ClientError("lost response")
        replies = [page("first", "second"), first, offline,
                   page("first", "second"), None, page("second"), second, None, page()]
        with patch.object(client, "request", side_effect=replies) as request, \
             patch.object(client, "play") as play:
            with self.assertRaisesRegex(client.ClientError, "lost response"):
                client.drain(self.path, self.state)
            reloaded = json.loads(self.path.read_text())
            self.assertEqual(reloaded["pending"], {"source": "first"})

            # When
            client.drain(self.path, reloaded)

        # Then
        self.assertEqual([call.args[0] for call in play.call_args_list], [first["url"], second["url"]])
        self.assertEqual([call.args[1] for call in request.call_args_list], [
            "/v1/audio", "/v1/audio/first/download", "/v1/audio/first/played",
            "/v1/audio", "/v1/audio/first/played", "/v1/audio",
            "/v1/audio/second/download", "/v1/audio/second/played", "/v1/audio",
        ])
        self.assertEqual(json.loads(self.path.read_text())["pending"], {})

    def test_given_playback_failure_when_draining_then_stops_before_acknowledging(self):
        # Given
        replies = [page("broken", "next"), {"url": "https://storage.test/broken.mp3"}]

        # When
        with patch.object(client, "request", side_effect=replies) as request, \
             patch.object(client, "play", side_effect=client.ClientError("decode failed")):
            with self.assertRaisesRegex(client.ClientError, "decode failed"):
                client.drain(self.path, self.state)

        # Then
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         ["/v1/audio", "/v1/audio/broken/download"])
        self.assertFalse(self.path.exists())
