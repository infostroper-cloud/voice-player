"""Публикация постов и анонсов релизов VoicePlayer в X (Twitter) через API v2 (OAuth 1.0a).

Читает X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
из ~/.voice-player-secrets.env (с фолбэком на переменные окружения).
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS_FILE = Path.home() / ".voice-player-secrets.env"
ROOT = Path(__file__).resolve().parent.parent


def load_x_secrets() -> dict:
    data = {}
    if SECRETS_FILE.is_file():
        for raw in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return {
        "api_key": data.get("X_API_KEY") or os.environ.get("X_API_KEY", ""),
        "api_secret": data.get("X_API_SECRET") or os.environ.get("X_API_SECRET", ""),
        "access_token": data.get("X_ACCESS_TOKEN") or os.environ.get("X_ACCESS_TOKEN", ""),
        "access_secret": data.get("X_ACCESS_TOKEN_SECRET") or os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
    }


def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~")


def _oauth1_header(method: str, url: str, creds: dict) -> str:
    oauth = {
        "oauth_consumer_key": creds["api_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    parsed = urllib.parse.urlsplit(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    all_params = sorted([(_pct(k), _pct(v)) for k, v in list(oauth.items()) + query_pairs])
    param_str = "&".join(f"{k}={v}" for k, v in all_params)
    base_str = f"{method.upper()}&{_pct(base_url)}&{_pct(param_str)}"
    signing_key = f"{_pct(creds['api_secret'])}&{_pct(creds['access_secret'])}"
    sig = base64.b64encode(hmac.new(signing_key.encode("ascii"), base_str.encode("ascii"), hashlib.sha1).digest()).decode("ascii")
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def x_call(method: str, url: str, creds: dict, payload: dict | None = None) -> dict:
    for k in ("api_key", "api_secret", "access_token", "access_secret"):
        if not creds.get(k):
            raise RuntimeError(f"Не заполнены ключи X API в {SECRETS_FILE}")
    headers = {"Authorization": _oauth1_header(method, url, creds)}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"X API HTTP {e.code}: {err_body}") from e


def post_tweet(text: str) -> dict:
    creds = load_x_secrets()
    return x_call("POST", "https://api.x.com/2/tweets", creds, {"text": text})


def default_release_tweet(version: str) -> str:
    return (
        f"🎙 VoicePlayer v{version} is out!\n\n"
        "• Local Kokoro-82M & Piper TTS (runs offline on CPU)\n"
        "• Auto-picks up Claude Code, Codex Desktop, DeepSeek & Antigravity\n"
        "• Preemptive queue yielding so short replies play instantly\n\n"
        f"https://github.com/infostroper-cloud/voice-player/releases/tag/voice-player-v{version}"
    )


def main():
    parser = argparse.ArgumentParser(description="Публикация постов в X (Twitter)")
    parser.add_argument("--check", action="store_true", help="Проверить OAuth 1.0a ключи X API")
    parser.add_argument("--release", action="store_true", help="Опубликовать анонс текущей версии из VERSION")
    parser.add_argument("--text", type=str, default="", help="Текст поста (до 280 символов)")
    parser.add_argument("--file", type=str, default="", help="Путь к файлу с текстом поста")
    args = parser.parse_args()

    creds = load_x_secrets()
    if args.check:
        res = x_call("GET", "https://api.x.com/2/users/me", creds)
        u = res.get("data", {})
        print(f"X OK: @{u.get('username')} (id={u.get('id')})")
        return

    if args.release:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        text = default_release_tweet(version)
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read().strip()

    if not text:
        raise SystemExit("Пустой текст поста")

    res = post_tweet(text)
    tid = res.get("data", {}).get("id")
    print(f"Опубликовано в X: tweet_id={tid}")


if __name__ == "__main__":
    main()
