import requests, time, os, threading
from datetime import datetime
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


threading.Thread(target=run_web_server, daemon=True).start()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing - set it in Render's environment variables")
if not CHAT_ID:
    raise ValueError("CHAT_ID missing - set it in Render's environment variables")

# CoinGecko uses "ripple" as the coin id for XRP
COINGECKO_ID = "ripple"
NAME = "XRP/USDT"

# XRP trades at low decimal values (~1.1xxxx), so we need more decimal
# places than the BTC bot used, or we lose all the meaningful precision.
DECIMALS = 5

history = deque(maxlen=60)
last_signal = "HOLD"

# Track signal history over time for percentage breakdown
# 30s per cycle, 3 hours = 360 readings
SIGNAL_WINDOW = 360
signal_history = deque(maxlen=SIGNAL_WINDOW)

# Interactive state
last_update_id = 0
snoozed_until = 0  # epoch timestamp; periodic updates paused while now < this

# Cache of the most recent price snapshot, so commands/buttons can reply instantly
latest = {"price": None, "change": None, "signal": "HOLD", "reason": "Building data..."}


def signal_percentages():
    if not signal_history:
        return None
    total = len(signal_history)
    buy_pct = (signal_history.count("BUY") / total) * 100
    sell_pct = (signal_history.count("SELL") / total) * 100
    hold_pct = (signal_history.count("HOLD") / total) * 100
    hours_covered = (total * 30) / 3600
    return buy_pct, sell_pct, hold_pct, hours_covered


def send(text, reply_markup=None):
    try:
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
            json=payload,
            timeout=10
        )
    except Exception as e:
        print("Send Error:", e, flush=True)


def answer_callback(callback_id, text=""):
    try:
        requests.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=10
        )
    except Exception as e:
        print("Answer Callback Error:", e, flush=True)


def refresh_buttons():
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Refresh", "callback_data": "refresh"},
                {"text": "😴 Snooze 1h", "callback_data": "snooze"}
            ]
        ]
    }


def build_status_message():
    if latest["price"] is None:
        return "Still building data, give it a moment..."

    msg = (
        NAME + "\n"
        "Price: " + str(round(latest["price"], DECIMALS)) + "\n"
        "24h Change: " + str(round(latest["change"], 2)) + "%\n"
        "Signal: " + latest["signal"] + "\n"
        "Reason: " + latest["reason"]
    )

    pct = signal_percentages()
    if pct:
        buy_pct, sell_pct, hold_pct, hours = pct
        msg += (
            "\n\nLast " + str(round(hours, 1)) + "h breakdown:\n"
            "BUY: " + str(round(buy_pct, 2)) + "%\n"
            "SELL: " + str(round(sell_pct, 2)) + "%\n"
            "HOLD: " + str(round(hold_pct, 2)) + "%"
        )
    return msg


def build_price_message():
    if latest["price"] is None:
        return "Still building data, give it a moment..."
    return NAME + " price: " + str(round(latest["price"], DECIMALS)) + " (" + str(round(latest["change"], 2)) + "% 24h)"


def handle_updates():
    global last_update_id, snoozed_until
    try:
        resp = requests.get(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 0},
            timeout=10
        ).json()
    except Exception as e:
        print("getUpdates Error:", e, flush=True)
        return

    if not resp.get("ok"):
        print("getUpdates not ok:", resp, flush=True)
        return

    results = resp.get("result", [])
    if results:
        print("Got", len(results), "update(s)", flush=True)

    for update in results:
        last_update_id = update["update_id"]

        try:
            # Handle typed commands
            if "message" in update:
                text = update["message"].get("text", "")
                print("Received message:", text, flush=True)
                if text == "/status":
                    send(build_status_message(), reply_markup=refresh_buttons())
                elif text == "/price":
                    send(build_price_message(), reply_markup=refresh_buttons())

            # Handle button taps
            elif "callback_query" in update:
                cq = update["callback_query"]
                data = cq.get("data")
                print("Received callback:", data, flush=True)
                if data == "refresh":
                    send(build_status_message(), reply_markup=refresh_buttons())
                    answer_callback(cq["id"], "Refreshed")
                elif data == "snooze":
                    snoozed_until = time.time() + 3600
                    answer_callback(cq["id"], "Snoozed for 1 hour")
                    send("Periodic updates snoozed for 1 hour. Use /status anytime.")
        except Exception as e:
            print("Error handling update:", e, flush=True)


def get_price():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": COINGECKO_ID, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10
        ).json()
        price = float(r[COINGECKO_ID]["usd"])
        change = float(r[COINGECKO_ID].get("usd_24h_change", 0))
        return price, change
    except:
        return None, None


def ma(arr, n):
    a = list(arr)
    return sum(a[-n:]) / n if len(a) >= n else None


def rsi(arr, p=14):
    a = list(arr)
    if len(a) < p + 1:
        return None
    g = 0
    l = 0
    for i in range(-p, 0):
        d = a[i] - a[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    ag = g / p
    al = l / p
    if al == 0:
        return 100
    return 100 - (100 / (1 + (ag / al)))


def signal(price, m5, m10, m30, r):
    if not all([m5, m10, m30]) or r is None:
        return "HOLD", "Building data..."
    bull = price > m5 and m5 > m10 and m10 > m30
    bear = price < m5 and m5 < m10 and m10 < m30
    if bull and r < 32:
        return "BUY", "Price above all MAs plus RSI oversold"
    if bull and r < 50:
        return "BUY", "Bullish MA alignment"
    if bear and r > 68:
        return "SELL", "Price below all MAs plus RSI overbought"
    if bear and r > 55:
        return "SELL", "Bearish MA stack"
    if r < 32 and price > m10:
        return "BUY", "RSI oversold plus above MA10"
    if r > 68 and price < m10:
        return "SELL", "RSI overbought plus below MA10"
    return "HOLD", "Watching..."


send(
    "XRP Bot Started! Now tracking XRP/USDT. Building data, wait 2 minutes for first signals.\n\n"
    "Commands: /status, /price"
)
print("Bot started - XRP/USDT only, interactive mode", flush=True)
counter = 0

while True:
    try:
        # Check for incoming commands/button taps every cycle
        handle_updates()

        price, change = get_price()
        if price is not None:
            history.append(price)
            m5 = ma(history, 5)
            m10 = ma(history, 10)
            m30 = ma(history, 30)
            r = rsi(history)
            sig, reason = signal(price, m5, m10, m30, r)
            signal_history.append(sig)

            latest["price"] = price
            latest["change"] = change
            latest["signal"] = sig
            latest["reason"] = reason

            rtxt = str(round(r, 1)) if r is not None else "..."
            print(datetime.now().strftime("%H:%M:%S") + " " + NAME + " price=" + str(price) + " signal=" + sig + " rsi=" + rtxt, flush=True)

            if sig != last_signal and sig != "HOLD":
                label = "BUY ALERT" if sig == "BUY" else "SELL ALERT"
                msg = label + " " + NAME + "\nPrice: " + str(round(price, DECIMALS)) + "\nChange: " + str(round(change, 2)) + "%\nReason: " + reason

                pct = signal_percentages()
                if pct:
                    buy_pct, sell_pct, hold_pct, hours = pct
                    msg += "\n\nLast " + str(round(hours, 1)) + "h breakdown:\nBUY: " + str(round(buy_pct, 2)) + "%\nSELL: " + str(round(sell_pct, 2)) + "%\nHOLD: " + str(round(hold_pct, 2)) + "%"

                send(msg, reply_markup=refresh_buttons())
            last_signal = sig
        else:
            print("Failed to fetch price", flush=True)

        counter += 1
        if counter >= 20:
            if len(history) > 0 and time.time() >= snoozed_until:
                send(build_status_message(), reply_markup=refresh_buttons())
            counter = 0

    except Exception as e:
        print("Error: " + str(e), flush=True)

    time.sleep(30)
