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
    from rich.prompt import Prompt, Confirm
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
    _print("[1/4] 安裝依賴套件...", "bold")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q",
        "python-telegram-bot>=21",
        "apscheduler>=3.10,<4.0",
    ])
    _print("  ✅ 完成")


def _setup_token() -> str:
    _print("\n[2/4] 建立 Bot Token", "bold")
    _print("  1. 在 Telegram 搜尋 [bold]@BotFather[/bold]")
    _print("  2. 發送 /newbot，依指示建立 Bot")
    _print("  3. 取得 Bot Token（格式：123456:ABC-DEF...）")
    if _con:
        token = Prompt.ask("  Bot Token").strip()
    else:
        token = input("  Bot Token: ").strip()
    return token


def _setup_chat_id(token: str) -> str:
    _print("\n[3/4] 設定推播目標（頻道 / 群組 / 個人）", "bold")
    _print("  選擇推播目標：")
    _print("  [bold]A[/bold] 私人頻道或群組（需先加 Bot 為管理員）")
    _print("  [bold]B[/bold] 公開頻道（有 @username）")
    _print("  [bold]C[/bold] 個人對話（Bot 直接傳給你）")

    if _con:
        choice = Prompt.ask("  選擇", choices=["A", "B", "C"], default="A").upper()
    else:
        choice = input("  選擇 [A/B/C]（預設 A）: ").strip().upper() or "A"

    if choice == "A":
        _print("\n  步驟：")
        _print("  1. 進入你的頻道/群組 → 管理員 → 新增管理員")
        _print("  2. 搜尋你的 Bot 名稱，加入並給「發送訊息」權限")
        _print("  3. 取得 Chat ID 方法：")
        _print("     · 將頻道任一訊息轉發給 [bold]@userinfobot[/bold]")
        _print("     · 它會回覆 chat.id（私人頻道格式：-1001234567890）")
        if _con:
            chat_id = Prompt.ask("  Chat ID（負數，如 -1001234567890）").strip()
        else:
            chat_id = input("  Chat ID: ").strip()

    elif choice == "B":
        _print("\n  步驟：")
        _print("  1. 進入你的公開頻道 → 管理員 → 新增管理員")
        _print("  2. 搜尋你的 Bot 名稱，加入並給「發送訊息」權限")
        if _con:
            username = Prompt.ask("  頻道 @username（不含 @）").strip().lstrip("@")
        else:
            username = input("  頻道 @username（不含 @）: ").strip().lstrip("@")
        chat_id = f"@{username}"

    else:  # C — personal
        _print("\n  步驟：")
        _print("  1. 在 Telegram 搜尋你的 Bot，發送任一訊息給它")
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        _print(f"  2. 開啟：{url}")
        _print("  3. 找到 message.chat.id（你的個人 Chat ID）")
        if _con:
            chat_id = Prompt.ask("  Chat ID").strip()
        else:
            chat_id = input("  Chat ID: ").strip()

    return chat_id


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
    token = _setup_token()
    chat_id = _setup_chat_id(token)

    _print("\n[4/4] 測試訊息發送中...", "bold")
    if _test_message(token, chat_id):
        _print("  ✅ 收到測試訊息")
    else:
        _print("  ⚠ 測試失敗，請確認：", "yellow")
        _print("    · Bot Token 是否正確", "yellow")
        _print("    · Bot 是否已被加為頻道/群組管理員", "yellow")
        _print("    · Chat ID / @username 是否正確", "yellow")
        if _con:
            if not Confirm.ask("  仍要寫入 .env？", default=False):
                _print("  已取消，請重新執行 make bot-setup", "red")
                return
        else:
            ans = input("  仍要寫入 .env？[y/N] ").strip().lower()
            if ans != "y":
                return

    _write_env(token, chat_id)
    _print("\n  ✅ 設定已寫入 .env")
    _print("\n設定完成！執行 [bold]make bot[/bold] 啟動機器人")


if __name__ == "__main__":
    main()
