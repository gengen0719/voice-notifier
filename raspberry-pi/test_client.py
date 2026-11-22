from http.client import HTTPResponse
from io import BytesIO, StringIO
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import client


def http_response(data):
    socket = Mock()
    socket.makefile.return_value = BytesIO(data)
    response = HTTPResponse(socket)
    response.begin()
    return response


class ClientTest(unittest.TestCase):
    def run_download(self, response):
        opener = Mock()
        opener.open.side_effect = [
            http_response(
                b'HTTP/1.1 200 OK\r\n\r\n'
                b'{"sourceId":"source","items":[{"audioId":"notice"}]}'
            ),
            http_response(
                b'HTTP/1.1 200 OK\r\n\r\n'
                b'{"url":"https://storage.test/notice.mp3"}'
            ),
            response,
        ]
        state = {"credentials": {"endpoint": "https://api.test", "token": "token"}}
        with patch.object(client, "OPENER", opener), \
             patch.object(client.subprocess, "run") as player, \
             patch.object(client, "save_state") as save:
            with self.assertRaises(client.ClientError):
                client.drain(Path("unused"), state)
        self.assertEqual([call.args[0].get_method() for call in opener.open.call_args_list],
                         ["GET", "POST", "GET"])
        self.assertEqual(state["pending"], {})
        save.assert_not_called()
        return player

    def test_empty_mp3_does_not_acknowledge(self):
        player = self.run_download(http_response(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))
        player.assert_not_called()

    def test_invalid_mp3_with_no_decoded_samples_does_not_acknowledge(self):
        player = self.run_download(http_response(
            b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\ninvalid"
        ))
        player.assert_called_once()
        self.assertIn("-s", player.call_args.args[0])

    def test_short_download_does_not_start_player_or_acknowledge(self):
        player = self.run_download(http_response(
            b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\npartial"
        ))
        player.assert_not_called()

    def test_truncated_chunked_download_does_not_acknowledge(self):
        player = self.run_download(http_response(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\npartial\r\n"
        ))
        player.assert_not_called()

    def test_truncated_api_response_is_retried(self):
        response = http_response(b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{"source')
        with patch.object(client.OPENER, "open", return_value=response), \
             patch.object(client.shutil, "which", return_value="mpg123"), \
             patch.object(client.time, "sleep", side_effect=KeyboardInterrupt) as sleep, \
             patch("sys.stderr", new_callable=StringIO) as stderr:
            with self.assertRaises(KeyboardInterrupt):
                client.run(Path("unused"), {"credentials": {"endpoint": "https://api.test", "token": "token"}}, 5)
        sleep.assert_called_once_with(5)
        self.assertIn("再試行", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
