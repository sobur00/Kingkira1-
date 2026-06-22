import os
import sys
import time
import logging
import threading
from collections import deque

import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Force unbuffered stdout so Render logs show up immediately
# ---------------------------------------------------------------------------
sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("btc_bot")

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PORT = int(os.environ.get("PORT", 10000))

if not BOT_TOKEN:
    log.error("Missing BOT_TOKEN env variable. Exiting.")
    sys.exit(1)
if not CHAT_ID:
    log.error("Missing CHAT_ID env variable. Exiting.")
    sys.exit(1)

CHAT_ID = int(CHAT_ID)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
VS_CURRENCY = "usd"

POLL_SECONDS = 30
HISTORY_MAXLEN = 60

MA_FAST = 5
MA_MID = 10
MA_SLOW = 30
RSI_PERIOD = 14

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
price_history = deque(maxlen=HISTORY_MAXLEN)
last_price = None
last_24h_change = None

consecutive_failures = 0
last_fail_reason = None

signal_history = deque(maxlen=360)  # ~3 hours at 30s polling
snooze_until = 0

state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------
def sma(values, period):
    if len(values) < period:
        return None
    window = list(values)[-period:]
    return sum(window) / period


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    vals = list(values)[-(period + 1):]
    gains = []
    losses = []
    for i in range(1, len(vals)):
        diff = vals[i] - vals[i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_signal():
    ma5 = sma(price_history, MA_FAST)
    ma10 = sma(price_history, MA_MID)
    ma30 = sma(price_history, MA_SLOW)
    r = rsi(price_history, RSI_PERIOD)

    if ma5 is None or ma10 is None or ma30 is None:
        return "HOLD", "Gathering data...", ma5, ma10, ma30, r

    if ma5 > ma10 > ma30 and r is not None and r < 70:
        return "BUY", "MA5>MA10>MA30 bullish alignment, RSI confirms", ma5, ma10, ma30, r
    if ma5 < ma10 < ma30 and r is not None and r > 30:
        return "SELL", "MA5<MA10<MA30 bearish alignment, RSI confirms", ma5, ma10, ma30, r

    return "HOLD", "Watching...", ma5, ma10, ma30, r


def breakdown_last_hours(hours=3.0):
    cutoff_count = int((hours * 3600) / POLL_SECONDS)
    recent = list(signal_history)[-cutoff_count:] if cutoff_count > 0 else list(signal_history)
    if not recent:
        return 0.0, 0.0, 0.0
    total = len(recent)
    buy = sum(1 for s in recent if s == "BUY") / total * 100
    sell = sum(1 for s in recent if s == "SELL") / total * 100
    hold = sum(1 for s in recent if s == "HOLD") / total * 100
    return buy, sell, hold


# ---------------------------------------------------------------------------
# CoinGecko fetcher
# ---------------------------------------------------------------------------
def fetch_price():
    params = {
        "ids": "bitcoin",
        "vs_currencies": VS_CURRENCY,
        "include_24hr_change": "true",
    }
    resp = requests.get(COINGECKO_PRICE_URL, params=params, timeout=10)
    if resp.status_code == 429:
        raise RuntimeError("429 rate limited")
    resp.raise_for_status()
    data = resp.json()
    price = data["bitcoin"]["usd"]
    change = data["bitcoin"].get("usd_24h_change")
    return price, change


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------
def poll_loop():
    global last_price, last_24h_change
    global consecutive_failures, last_fail_reason

    while True:
        try:
            price, change = fetch_price()

            with state_lock:
                if price:
                    price_history.append(price)
                last_price = price
                last_24h_change = change
                consecutive_failures = 0
                last_fail_reason = None

                sig, _, *_ = compute_signal()
                signal_history.append(sig)

            log.info(
                "Poll OK | price=%.2f change=%.2f%%",
                price, change if change is not None else 0.0,
            )

        except Exception as e:
            with state_lock:
                consecutive_failures += 1
                last_fail_reason = str(e)
            log.warning(
                "Price fetch failing for BTC/USDT | Reason: %s | Consecutive failures: %d",
                last_fail_reason, consecutive_failures,
            )
            if consecutive_failures > 0 and consecutive_failures % 10 == 0:
                notify_failure_sync()

        time.sleep(POLL_SECONDS)


def notify_failure_sync():
    """Fire-and-forget warning message to chat about repeated failures."""
    try:
        import asyncio
        asyncio.run(_send_failure_message())
    except Exception as e:
        log.error("Failed to send failure notice: %s", e)


async def _send_failure_message():
    if time.time() < snooze_until:
        return
    text = (
        f"⚠️ Price fetch failing for BTC/USDT\n"
        f"Reason: {last_fail_reason}\n"
        f"Consecutive failures: {consecutive_failures}"
    )
    await application.bot.send_message(chat_id=CHAT_ID, text=text)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def build_status_message():
    with state_lock:
        price = last_price
        change = last_24h_change
        sig, reason, ma5, ma10, ma30, r = compute_signal()
        buy_pct, sell_pct, hold_pct = breakdown_last_hours(3.0)

    if price is None:
        return "No price data yet. Still gathering data..."

    change_str = f"{change:.2f}%" if change is not None else "N/A"

    msg = (
        f"BTC/USDT\n"
        f"Price: {price:.1f}\n"
        f"24h Change: {change_str}\n"
        f"Signal: {sig}\n"
        f"Reason: {reason}\n\n"
        f"Last 3.0h breakdown:\n"
        f"BUY: {buy_pct:.2f}%\n"
        f"SELL: {sell_pct:.2f}%\n"
        f"HOLD: {hold_pct:.2f}%"
    )
    return msg


def build_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
            InlineKeyboardButton("⏰ Snooze 1h", callback_data="snooze"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_status_message(), reply_markup=build_keyboard())


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with state_lock:
        price = last_price
    if price is None:
        await update.message.reply_text("No price data yet.")
        return
    await update.message.reply_text(f"BTC/USDT: {price:.1f}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global snooze_until
    query = update.callback_query
    await query.answer()

    if query.data == "refresh":
        await query.edit_message_text(
            build_status_message(), reply_markup=build_keyboard()
        )
    elif query.data == "snooze":
        snooze_until = time.time() + 3600
        await query.answer("Snoozed for 1 hour", show_alert=True)


# ---------------------------------------------------------------------------
# Flask self-ping server (keeps Render free tier awake)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "BTC bot alive", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


def self_ping_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        log.info("RENDER_EXTERNAL_URL not set, skipping self-ping.")
        return
    while True:
        try:
            requests.get(url, timeout=10)
            log.info("Self-ping OK")
        except Exception as e:
            log.warning("Self-ping failed: %s", e)
        time.sleep(600)  # every 10 min


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("price", cmd_price))
application.add_handler(CallbackQueryHandler(on_button))


def main():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()

    log.info("BTC bot starting polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
