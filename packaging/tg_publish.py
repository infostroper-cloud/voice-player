"""Публикация анонсов релизов и новостей в Telegram-канал проекта (@voiceplayer_ai).

Читает TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL из ~/.voice-player-secrets.env
(с фолбэком на переменные окружения). Никаких секретов в репозитории не хранит.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS_FILE = Path.home() / ".voice-player-secrets.env"
ROOT = Path(__file__).resolve().parent.parent


def load_secrets() -> dict:
    """Прочитать пары KEY=VALUE из ~/.voice-player-secrets.env и окружения."""
    data = {}
    if SECRETS_FILE.is_file():
        for raw in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    token = data.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel = data.get("TELEGRAM_CHANNEL") or os.environ.get("TELEGRAM_CHANNEL", "@voiceplayer_ai")
    return {"token": token, "channel": channel}


def tg_call(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload is None:
        req = urllib.request.Request(url)
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {e.code}: {err_body}") from e


def send_post(text: str, channel: str = "", parse_mode: str = "HTML", disable_preview: bool = False) -> dict:
    cfg = load_secrets()
    token = cfg["token"]
    chat_id = channel or cfg["channel"]
    if not token:
        raise RuntimeError(f"Не задан TELEGRAM_BOT_TOKEN (укажите в {SECRETS_FILE})")
    return tg_call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        },
    )


def default_release_text(version: str) -> str:
    return (
        f"🎙 <b>VoicePlayer v{version} — локальная озвучка уровня Speechify, Codex Desktop и быстрая очередь</b>\n\n"
        "Вышло крупное обновление! Главное из последних релизов:\n\n"
        "✨ <b>Локальные HD-голоса Kokoro-82M (Света HD, Маша HD, Дима HD)</b>\n"
        "Естественная нейросетевая озвучка прямо на вашем ПК без интернета и серверов Microsoft — "
        "модель и словарь ударений скачиваются автоматически при первом выборе голоса.\n\n"
        "🤖 <b>Авто-подхват облачных диалогов Codex Desktop</b>\n"
        "VoicePlayer теперь автоматически подтягивает не только локальные сессии Claude Code, DeepSeek, "
        "Ollama, Kimi и Antigravity, но и облачные треды из окна Codex Desktop.\n\n"
        "⚡ <b>Умная очередь без подвисаний</b>\n"
        "Длинные ответы больше не блокируют короткие и новые реплики: рендер идёт по частям с вытеснением — "
        "свежая или выбранная реплика начинает звучать сразу.\n\n"
        "🛡 <b>Стабильность и архитектура</b>\n"
        "Устранён конфликт локальных движков Piper и Kokoro, переработан рендер на лету при смене голоса.\n\n"
        f"📥 Скачать обновление: <a href=\"https://github.com/infostroper-cloud/voice-player/releases/tag/voice-player-v{version}\">"
        f"GitHub Releases (v{version})</a>\n"
        "🌐 Сайт: https://infostroper-cloud.github.io/voice-player/"
    )


def main():
    parser = argparse.ArgumentParser(description="Публикация постов в Telegram-канал VoicePlayer")
    parser.add_argument("--check", action="store_true", help="Проверить токен бота и доступ к каналу")
    parser.add_argument("--release", action="store_true", help="Опубликовать анонс текущей версии из VERSION")
    parser.add_argument("--text", type=str, default="", help="Текст сообщения (HTML)")
    parser.add_argument("--file", type=str, default="", help="Путь к файлу с текстом поста")
    parser.add_argument("--channel", type=str, default="", help="Переопределить канал/chat_id")
    args = parser.parse_args()

    cfg = load_secrets()
    if args.check:
        me = tg_call(cfg["token"], "getMe")
        ch = tg_call(cfg["token"], f"getChat?chat_id={urllib.parse.quote(args.channel or cfg['channel'])}")
        print("BOT:", me["result"]["username"], "CHANNEL:", ch["result"]["title"])
        return

    if args.release:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        text = default_release_text(version)
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read().strip()

    if not text:
        raise SystemExit("Пустой текст поста")

    res = send_post(text, channel=args.channel)
    msg = res.get("result", {})
    print(f"Опубликовано в {args.channel or cfg['channel']}: message_id={msg.get('message_id')}")


if __name__ == "__main__":
    main()
