"""One-time interactive setup: Telegram token + Chat ID → .env

Usage:
    python scripts/bot_setup.py
    make bot-setup
"""
from __future__ import annotations

import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from rich.console import Console
    from rich.panel import Panel
    _con: Console | None = Console()
except ImportError:
    _con = None

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _print(msg: str, style: str = "") -> None:
    if _con:
        _con.print(msg, style=style)
    else:
        print(msg)


def _install_deps() -> None:
    _print("[1/3] 安裝依賴套件...", "bold")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q",
        "python-telegram-bot>=21",
        "apscheduler>=3.10,<4.0",
    ])
    _print("  ✅ 完成")


def _setup_telegram() -> tuple[str, str]:
    _print("\n[2/3] Telegram 設定", "bold")
    _print("  請先在 Telegram 搜尋 @BotFather 建立 Bot，取得 token")
    token = input("  Bot Token: ").strip()
    _print("  請在 Telegram 傳一則訊息給 Bot，然後開啟：")
    _print(f"  https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates")
    _print("  找到 message.chat.id（你的 Chat ID）")
    chat_id = input("  Chat ID: ").strip()
    return token, chat_id


def _test_message(token: str, chat_id: str) -> bool:
    text = "✅ 股票信號機器人設定完成！"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        _print(f"  ❌ 發送失敗：{e}", "red")
        return False


def _write_env(token: str, chat_id: str) -> None:
    existing = _ENV_PATH.read_text() if _ENV_PATH.exists() else ""
    lines = [l for l in existing.splitlines() if not l.startswith("TELEGRAM_")]
    lines += [f"TELEGRAM_BOT_TOKEN={token}", f"TELEGRAM_CHAT_ID={chat_id}"]
    _ENV_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    if _con:
        _con.print(Panel("股票信號機器人 — 初始設定", style="bold blue"))

    _install_deps()
    token, chat_id = _setup_telegram()

    _print("\n  測試訊息發送中...", "dim")
    if _test_message(token, chat_id):
        _print("  ✅ 收到測試訊息")
    else:
        _print("  ⚠ 測試失敗，請確認 token 和 chat_id 正確", "yellow")

    _print("\n[3/3] 寫入 .env...", "bold")
    _write_env(token, chat_id)
    _print("  ✅ 完成\n")
    _print("設定完成！執行 [bold]make bot[/bold] 啟動機器人")


if __name__ == "__main__":
    main()
