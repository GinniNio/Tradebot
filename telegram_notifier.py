"""
telegram_notifier.py — Push alerts to Kaye's phone via Telegram Bot API.

Why this exists: the dashboard is pull-only (localhost:3001). When a tracker
fails silently (see: graduation tracker dead 2026-06-05 to 2026-06-11 with
nobody noticing), nothing pings the operator. This module pushes three kinds
of events:

  1. Signal fires        — every signal_outcomes row created (via the
                           record_signal_outcome choke point)
  2. Tracker failures    — throttled error alerts (e.g. pump.fun API dead)
  3. Ad-hoc notices      — anything else worth a phone buzz

Setup (one-time, ~3 minutes):
  1. In Telegram, message @BotFather -> /newbot -> pick a name. Copy the token.
  2. Open a chat with your new bot and press Start (required or sends fail).
  3. Get your chat id: message @userinfobot, or after step 2 run:
       python telegram_notifier.py --get-chat-id
  4. Put both in .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-...
       TELEGRAM_CHAT_ID=123456789
  5. Verify: python telegram_notifier.py "hello from tradebot"
  6. Restart the bot.

Design notes:
  - Fail-silent: a Telegram outage must NEVER break signal recording. Every
    network call is wrapped; failures log a WARNING and return False.
  - No-op when unconfigured: if token/chat_id are missing, every function
    returns False immediately. Safe to wire into code paths before setup.
  - Throttled error alerts: notify_error dedupes on a key so a tracker
    failing every 60s sends one alert per ERROR_THROTTLE_SEC, not 1440/day.
  - ASCII-only log strings (cp1252 console). Message text sent to Telegram
    can be anything; log lines cannot.
"""

import asyncio
import logging
import sys
import time

import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
SEND_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Minimum seconds between repeat alerts for the same error key.
ERROR_THROTTLE_SEC = 6 * 3600

# key -> unix ts of last sent alert
_last_error_sent: dict[str, float] = {}


def telegram_enabled() -> bool:
    """True when both token and chat id are configured."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


async def send_message(text: str, silent: bool = False) -> bool:
    """
    Send a plain-text message to the configured chat.

    Returns True on success, False on any failure or when unconfigured.
    Never raises — callers must be able to fire-and-forget.
    """
    if not telegram_enabled():
        return False
    url = f"{API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],  # Telegram hard limit is 4096
        "disable_notification": silent,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=SEND_TIMEOUT) as resp:
                if resp.status == 200:
                    return True
                body = (await resp.text())[:200]
                logger.warning(
                    "Telegram send failed: HTTP %d body=%s", resp.status, body
                )
                return False
    except Exception as e:
        logger.warning("Telegram send failed: %r", e)
        return False


def send_message_nowait(text: str, silent: bool = False) -> None:
    """
    Fire-and-forget from sync code running inside an event loop.
    No-op if no loop is running (e.g. CLI scripts) or unconfigured.
    """
    if not telegram_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(send_message(text, silent=silent))


def notify_signal(
    strategy: str,
    symbol: str,
    token_address: str,
    chain: str,
    price: float,
    route_available: "bool | None",
) -> None:
    """One-line signal-fire push. Called from record_signal_outcome."""
    route_str = "?" if route_available is None else ("yes" if route_available else "NO")
    text = (
        f"SIGNAL  {strategy}\n"
        f"{symbol}  ({chain})\n"
        f"price ${price:.8f}  route={route_str}\n"
        f"{token_address}"
    )
    send_message_nowait(text)


def notify_error(key: str, text: str) -> None:
    """
    Throttled error push. `key` identifies the error class; repeats within
    ERROR_THROTTLE_SEC are dropped so a looping failure can't spam the chat.
    """
    now = time.time()
    last = _last_error_sent.get(key, 0)
    if now - last < ERROR_THROTTLE_SEC:
        return
    _last_error_sent[key] = now
    send_message_nowait(f"TRADEBOT ALERT\n{text}")


# ─── CLI: setup verification ──────────────────────────────────────────────────

async def _get_chat_id() -> None:
    """Print chat ids seen in the bot's recent updates (user must have
    pressed Start in the bot chat first)."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return
    url = f"{API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=SEND_TIMEOUT) as resp:
            data = await resp.json(content_type=None)
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("No updates found. Open your bot chat in Telegram, press Start,")
        print("send any message, then run this again.")
        return
    for cid, name in chats.items():
        print(f"chat_id: {cid}  ({name})  -> put TELEGRAM_CHAT_ID={cid} in .env")


async def _cli_send(text: str) -> None:
    if not telegram_enabled():
        print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and")
        print("TELEGRAM_CHAT_ID in .env first. See module docstring for steps.")
        return
    ok = await send_message(text)
    print("sent ok" if ok else "send FAILED (see log / check token + chat id)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--get-chat-id":
        asyncio.run(_get_chat_id())
    else:
        msg = sys.argv[1] if len(sys.argv) > 1 else "tradebot telegram test"
        asyncio.run(_cli_send(msg))
