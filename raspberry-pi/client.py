#!/usr/bin/env python3
"""Poll Voice Notifier and play queued MP3 files on Raspberry Pi OS."""

import argparse
import fcntl
from getpass import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ClientError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def https_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.fragment):
        raise ClientError("HTTPS の URL を指定してください。")
    return parts


def request(endpoint, path, method="GET", token=None, body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = Request(endpoint + path, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=20) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except HTTPError as error:
        error.close()
        raise
    except (OSError, ValueError) as error:
        raise ClientError("通知 API との通信または応答の読み取りに失敗しました。") from error


def save_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def register(path, state, endpoint):
    endpoint = endpoint.strip().rstrip("/")
    if https_url(endpoint).query:
        raise ClientError("HttpUrl にクエリは指定できません。")
    code = getpass("登録コード: ").strip()
    if not code:
        raise ClientError("登録コードを入力してください。")
    enrollment = state.get("enrollment")
    if not enrollment or enrollment["endpoint"] != endpoint:
        enrollment = {"endpoint": endpoint, "token": secrets.token_hex(32)}
        state["enrollment"] = enrollment
        save_state(path, state)
    device = request(endpoint, "/v1/devices", "POST",
                     body={"code": code, "token": enrollment["token"]})
    credentials = {**enrollment, "deviceId": device["deviceId"]}
    if credentials != state.get("credentials"):
        state["pending"] = {}
    state["credentials"] = credentials
    del state["enrollment"]
    save_state(path, state)
    print("端末を登録しました。", flush=True)


def play(url):
    https_url(url)
    try:
        with tempfile.TemporaryDirectory(prefix="voice-notifier-") as directory:
            audio = Path(directory) / "voice.mp3"
            with OPENER.open(Request(url), timeout=60) as response, audio.open("wb") as file:
                shutil.copyfileobj(response, file)
            subprocess.run(["mpg123", "-q", str(audio)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, HTTPError):
            error.close()
        raise ClientError("音声のダウンロードまたは再生に失敗しました。") from error


def drain(path, state):
    credentials = state["credentials"]
    endpoint, token = credentials["endpoint"], credentials["token"]
    pending = state.setdefault("pending", {})

    def acknowledge(audio_id):
        request(endpoint, f"/v1/audio/{audio_id}/played", "PUT", token=token)

    while True:
        page = request(endpoint, "/v1/audio", token=token)
        source = page["sourceId"]
        if source in pending:
            acknowledge(pending[source])
            del pending[source]
            save_state(path, state)
            continue
        if not page["items"]:
            return
        for item in page["items"]:
            audio_id = item["audioId"]
            try:
                signed = request(endpoint, f"/v1/audio/{audio_id}/download", "POST", token=token)
            except HTTPError as error:
                if error.code == 410:
                    continue
                raise
            play(signed["url"])
            pending[source] = audio_id
            save_state(path, state)
            acknowledge(audio_id)
            del pending[source]
            save_state(path, state)


def run(path, state, interval):
    if not state.get("credentials"):
        raise ClientError("先に register で端末を登録してください。")
    if shutil.which("mpg123") is None:
        raise ClientError("mpg123 をインストールしてください。")
    while True:
        try:
            drain(path, state)
        except HTTPError as error:
            if error.code == 401:
                raise ClientError("端末認証が失効しました。新しい登録コードで再登録してください。") from error
            print(f"通知 API の HTTP エラー ({error.code})。再試行します。", file=sys.stderr, flush=True)
        except ClientError as error:
            print(f"{error} 再試行します。", file=sys.stderr, flush=True)
        time.sleep(interval)


def main():
    default_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_dir / "voice-notifier")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register").add_argument("http_url", help="CDK の HttpUrl")
    commands.add_parser("run").add_argument("--interval", type=int, default=5, help="ポーリング間隔（秒）")
    args = parser.parse_args()
    if args.command == "run" and args.interval < 1:
        parser.error("--interval は 1 秒以上にしてください。")

    os.umask(0o077)
    try:
        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = args.state_dir / "state.json"
        with (args.state_dir / "client.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ClientError("この状態ディレクトリを別のクライアントが使用中です。") from error
            state = json.loads(path.read_text()) if path.exists() else {}
            if args.command == "register":
                register(path, state, args.http_url)
            else:
                run(path, state, args.interval)
    except KeyboardInterrupt:
        return 0
    except HTTPError as error:
        print(f"通知 API の HTTP エラー ({error.code})。", file=sys.stderr)
    except ClientError as error:
        print(error, file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("状態ファイル、URL、または API 応答を処理できませんでした。", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
