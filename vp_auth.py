# -*- coding: utf-8 -*-
"""Авторизация пользователей (Google OAuth 2.0 PKCE + регистрация контактов для рассылок)."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import platform
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional

_log = lambda msg: None

ACCOUNT_FILE: Path = Path.home() / ".voice-player" / "account.json"

# Заполняется своими значениями из Google Cloud Console (Desktop App OAuth Client)
# и Supabase / Cloudflare Worker для сохранения базы email-адресов:
GOOGLE_CLIENT_ID: str = os.environ.get("VP_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.environ.get("VP_GOOGLE_CLIENT_SECRET", "")
LEAD_WEBHOOK_URL: str = os.environ.get("VP_LEAD_WEBHOOK_URL", "")
LEAD_WEBHOOK_KEY: str = os.environ.get("VP_LEAD_WEBHOOK_KEY", "")


def set_logger(fn) -> None:
    global _log
    _log = fn


def log_line(msg: str) -> None:
    _log(msg)


def configure(
    *,
    account_file: Optional[Path] = None,
    google_client_id: Optional[str] = None,
    google_client_secret: Optional[str] = None,
    lead_webhook_url: Optional[str] = None,
    lead_webhook_key: Optional[str] = None,
) -> None:
    global ACCOUNT_FILE, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, LEAD_WEBHOOK_URL, LEAD_WEBHOOK_KEY
    if account_file is not None:
        ACCOUNT_FILE = Path(account_file)
    if google_client_id is not None:
        GOOGLE_CLIENT_ID = google_client_id.strip()
    if google_client_secret is not None:
        GOOGLE_CLIENT_SECRET = google_client_secret.strip()
    if lead_webhook_url is not None:
        LEAD_WEBHOOK_URL = lead_webhook_url.strip()
    if lead_webhook_key is not None:
        LEAD_WEBHOOK_KEY = lead_webhook_key.strip()


def load_account() -> dict:
    try:
        if ACCOUNT_FILE.is_file():
            data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_account(data: dict) -> None:
    try:
        ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log_line(f"vp_auth save error: {e}")


def clear_account() -> None:
    try:
        if ACCOUNT_FILE.exists():
            ACCOUNT_FILE.unlink()
    except Exception:
        pass


def is_signed_in() -> bool:
    acc = load_account()
    return bool(acc.get("email"))


def should_prompt_onboarding() -> bool:
    acc = load_account()
    return not acc.get("email") and not acc.get("skipped")


def mark_skipped() -> None:
    acc = load_account()
    acc["skipped"] = True
    acc["skipped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_account(acc)


def sync_lead_to_server(profile: dict, app_version: str = "") -> bool:
    """Отправить профиль пользователя на маркетинговый webhook / Supabase (если настроен)."""
    if not LEAD_WEBHOOK_URL or not profile.get("email"):
        return False
    payload = {
        "email": profile.get("email", ""),
        "name": profile.get("name", ""),
        "google_id": profile.get("sub", ""),
        "provider": profile.get("provider", "google"),
        "marketing_opt_in": bool(profile.get("marketing_opt_in", True)),
        "app_version": app_version,
        "os": platform.system(),
        "created_at": profile.get("logged_in_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    headers = {"Content-Type": "application/json", "User-Agent": "VoicePlayer/1.2"}
    if LEAD_WEBHOOK_KEY:
        headers["apikey"] = LEAD_WEBHOOK_KEY
        headers["Authorization"] = f"Bearer {LEAD_WEBHOOK_KEY}"
        headers["Prefer"] = "resolution=merge-duplicates"
    try:
        req = urllib.request.Request(
            LEAD_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        log_line(f"vp_auth lead sync error: {e}")
        return False


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def start_google_oauth(
    *,
    marketing_opt_in: bool = True,
    app_version: str = "",
    timeout_s: int = 180,
    on_done: Optional[Callable[[bool, str, dict], None]] = None,
) -> None:
    """Запустить Google OAuth 2.0 (PKCE) через системный браузер в фоновом потоке."""
    if not GOOGLE_CLIENT_ID:
        if on_done:
            on_done(False, "GOOGLE_CLIENT_ID не настроен (используйте вход по email или укажите VP_GOOGLE_CLIENT_ID)", {})
        return

    def worker():
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        result: dict = {"code": "", "error": ""}
        done_evt = threading.Event()

        class _CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/oauth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("state", [""])[0] != state:
                    result["error"] = "Неверный OAuth state"
                elif "error" in qs:
                    result["error"] = qs["error"][0]
                else:
                    result["code"] = qs.get("code", [""])[0]

                ok_msg = "Вход выполнен! Это окно можно закрыть и вернуться в VoicePlayer."
                err_msg = f"Ошибка входа: {html.escape(result['error'])}"
                body = (
                    "<!doctype html><html><meta charset='utf-8'>"
                    "<body style='font-family:system-ui,sans-serif;text-align:center;padding:60px;"
                    "background:#0b1020;color:#eef2ff'>"
                    f"<h2>{ok_msg if not result['error'] else err_msg}</h2>"
                    "</body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done_evt.set()

        try:
            srv = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        except Exception as e:
            if on_done:
                on_done(False, f"Не удалось открыть локальный callback: {e}", {})
            return

        port = srv.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/oauth/callback"
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "select_account",
        }
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
        threading.Thread(target=srv.serve_forever, daemon=True, name="oauth-cb").start()
        try:
            webbrowser.open(auth_url)
            if not done_evt.wait(timeout_s):
                if on_done:
                    on_done(False, "Время ожидания входа истекло", {})
                return
            if result["error"] or not result["code"]:
                if on_done:
                    on_done(False, result["error"] or "Код авторизации не получен", {})
                return

            token_form = {
                "client_id": GOOGLE_CLIENT_ID,
                "code": result["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
            if GOOGLE_CLIENT_SECRET:
                token_form["client_secret"] = GOOGLE_CLIENT_SECRET
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=urllib.parse.urlencode(token_form).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                tokens = json.loads(resp.read().decode("utf-8"))
            access_token = tokens.get("access_token", "")
            if not access_token:
                if on_done:
                    on_done(False, "Не получен access_token от Google", {})
                return

            u_req = urllib.request.Request(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(u_req, timeout=15) as u_resp:
                uinfo = json.loads(u_resp.read().decode("utf-8"))

            profile = {
                "provider": "google",
                "sub": str(uinfo.get("sub") or ""),
                "email": str(uinfo.get("email") or ""),
                "name": str(uinfo.get("name") or ""),
                "picture": str(uinfo.get("picture") or ""),
                "marketing_opt_in": bool(marketing_opt_in),
                "logged_in_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            save_account(profile)
            sync_lead_to_server(profile, app_version=app_version)
            if on_done:
                on_done(True, "", profile)
        except Exception as e:
            if on_done:
                on_done(False, str(e), {})
        finally:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True, name="google-oauth").start()


def register_email_profile(email: str, name: str = "", *, marketing_opt_in: bool = True, app_version: str = "") -> dict:
    """Локальная регистрация по email + отправка на сервер рассылки (резерв, если нет Google Client ID)."""
    profile = {
        "provider": "email",
        "email": email.strip(),
        "name": name.strip(),
        "marketing_opt_in": bool(marketing_opt_in),
        "logged_in_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_account(profile)
    threading.Thread(target=sync_lead_to_server, args=(profile, app_version), daemon=True).start()
    return profile
