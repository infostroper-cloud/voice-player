"""Модуль анонимной телеметрии, аналитики использования и монетизации VoicePlayer.

Принципы:
1. Privacy-First: никаких персональных данных, текстов диалогов, путей к файлам.
   Только обезличенный client_id (случайный UUIDv4) и агрегированные счётчики.
2. Подсчёт DAU/MAU через Google Analytics 4 Measurement Protocol и Cloudflare Worker.
3. Интеграция с дистанционным манифестом спонсоров/рекламы (sponsors.json)
   для последующей монетизации с возможностью отключения для Pro-пользователей.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

# Удалённый конфиг спонсоров / партнёров (обновляется через GitHub или CDN)
SPONSORS_URL = os.environ.get(
    "VP_SPONSORS_URL",
    "https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/sponsors.json"
)

# Опциональный эндпоинт приёма телеметрии (Cloudflare Worker или собственный бэкенд)
TELEMETRY_URL = os.environ.get(
    "VP_TELEMETRY_URL",
    "https://voiceplayer-telemetry.workers.dev/api/ping"
)

# Google Analytics 4 Measurement Protocol
# Чтобы подключить свой поток: создайте поток в GA4 (Администратор -> Потоки данных -> Веб / Приложение),
# скопируйте «Идентификатор потока данных» (Measurement ID, например G-XXXXXXXXXX)
# и «Секретный ключ API протокола Measurement Protocol» (API Secret).
GA4_MEASUREMENT_ID = os.environ.get("VP_GA4_MEASUREMENT_ID", "G-EBB4SZW3E5")
GA4_API_SECRET = os.environ.get("VP_GA4_API_SECRET", "")


class TelemetryManager:
    def __init__(self, prefs: dict, app_version: str, is_supporter_fn=lambda: False):
        self.prefs = prefs
        self.app_version = app_version
        self.is_supporter_fn = is_supporter_fn
        self.client_id = self._init_client_id()
        self._last_ping_ts = float(self.prefs.get("telemetry_last_ping", 0.0))
        self._last_sponsor_fetch = 0.0
        self.sponsor_data: Optional[dict[str, Any]] = None

    def _init_client_id(self) -> str:
        cid = self.prefs.get("client_id")
        if not cid or len(str(cid)) < 16:
            cid = str(uuid4())
            self.prefs["client_id"] = cid
        return str(cid)

    def record_session_start(self):
        """Увеличить счётчик запусков сессий и отправить событие открытия в GA4."""
        sessions = int(self.prefs.get("stats_sessions", 0)) + 1
        self.prefs["stats_sessions"] = sessions

        def _send():
            self._send_ga4_event("app_open", {
                "app_version": self.app_version,
                "platform": "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux"),
                "session_number": sessions,
            })
        threading.Thread(target=_send, daemon=True, name="ga4-open").start()

    def should_ping(self) -> bool:
        """Отправлять пинг не чаще 1 раза в 20 часов."""
        now = time.time()
        return (now - self._last_ping_ts) >= (20 * 3600)

    def _send_ga4_event(self, event_name: str, params: dict[str, Any]):
        """Отправить событие напрямую в Google Analytics 4 Measurement Protocol."""
        meas_id = GA4_MEASUREMENT_ID
        api_sec = GA4_API_SECRET
        if not meas_id or "XXXX" in meas_id:
            return
        url = f"https://www.google-analytics.com/mp/collect?measurement_id={meas_id}"
        if api_sec:
            url += f"&api_secret={api_sec}"
        body = {
            "client_id": self.client_id,
            "events": [
                {
                    "name": event_name,
                    "params": {
                        "engagement_time_msec": 1000,
                        **params
                    }
                }
            ]
        }
        try:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json", "User-Agent": f"VoicePlayer/{self.app_version}"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                resp.read()
        except Exception:
            pass

    def build_payload(self, total_seconds: float, total_items: int,
                      active_sources: list[str], voice: str, lang: str) -> dict[str, Any]:
        """Сформировать обезличенный пакет телеметрии."""
        os_name = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
        return {
            "client_id": self.client_id,
            "version": self.app_version,
            "os": os_name,
            "os_release": platform.release(),
            "arch": platform.machine(),
            "lang": lang,
            "voice": str(voice),
            "is_pro": bool(self.is_supporter_fn()),
            "stats": {
                "total_seconds": round(float(total_seconds), 1),
                "total_items": int(total_items),
                "sessions_count": int(self.prefs.get("stats_sessions", 1)),
                "sources_used": sorted(list(set(active_sources)))[:10],
            },
            "ts": int(time.time()),
        }

    def ping_async(self, total_seconds: float, total_items: int,
                   active_sources: list[str], voice: str, lang: str):
        """Отправить анонимный дневной heartbeat в фоновом потоке."""
        if not self.should_ping():
            return
        payload = self.build_payload(total_seconds, total_items, active_sources, voice, lang)

        def _worker():
            try:
                self._last_ping_ts = time.time()
                self.prefs["telemetry_last_ping"] = self._last_ping_ts

                # 1. Отправка в GA4 (DAU/MAU)
                self._send_ga4_event("app_heartbeat", {
                    "app_version": self.app_version,
                    "platform": payload["os"],
                    "lang": str(lang),
                    "voice": str(voice)[:32],
                    "total_seconds": round(float(total_seconds), 1),
                    "total_items": int(total_items),
                    "is_pro": int(bool(self.is_supporter_fn())),
                })

                # 2. Отправка в собственный телеметрический шлюз (если настроен)
                if TELEMETRY_URL and "workers.dev" not in TELEMETRY_URL or os.environ.get("VP_TELEMETRY_URL"):
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        TELEMETRY_URL,
                        data=data,
                        headers={"Content-Type": "application/json", "User-Agent": f"VoicePlayer/{self.app_version}"}
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        resp.read()
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="telemetry-ping").start()

    def fetch_sponsor_campaign_async(self, on_sponsor_loaded=None, force: bool = False):
        """Загрузить актуальную спонсорскую кампанию (для бесплатных пользователей)."""
        now = time.time()
        if not force and now - self._last_sponsor_fetch < 4 * 3600:
            if self.sponsor_data and on_sponsor_loaded:
                on_sponsor_loaded(self.sponsor_data)
            return

        def _worker():
            try:
                self._last_sponsor_fetch = time.time()
                req = urllib.request.Request(
                    SPONSORS_URL,
                    headers={"User-Agent": f"VoicePlayer/{self.app_version}"}
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get("active"):
                        self.sponsor_data = data
                        if on_sponsor_loaded:
                            on_sponsor_loaded(data)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="sponsor-fetch").start()
