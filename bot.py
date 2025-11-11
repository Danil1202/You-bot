import os
import asyncio
import json
import logging
import time
import threading
from collections import deque, defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

import pandas as pd
import pandas_ta as ta
import websockets
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ================= Логи =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("binary_signal_bot")

# ================= Константы =================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY")

PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "AUD/JPY", "EUR/GBP",
    "EUR/JPY", "GBP/JPY", "USD/CHF", "AUD/USD", "NZD/USD",
    "EUR/RUB", "USD/RUB"
]
TIMES = ["5 сек", "15 сек", "30 сек", "1 мин", "5 мин", "10 мин"]
TIME_BUTTONS = [TIMES[i:i+3] for i in range(0, len(TIMES), 3)]
PAIR_BUTTONS = [PAIRS[i:i+3] for i in range(0, len(PAIRS), 3)]

user_state = {}
auto_running = False
ws_task = None
prices = defaultdict(lambda: deque(maxlen=120))
last_sent = {}
SIGNAL_THRESHOLD = 0.3
COOLDOWN = 30

# ================= Сервер для Render =================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

port = int(os.environ.get("PORT", 8000))
server = HTTPServer(("0.0.0.0", port), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

# ================= Анализ =================
def compute_score(series):
    if len(series) < 10:
        return 0, ["мало данных"]
    df = pd.DataFrame({"close": series})
    df["ema5"] = ta.ema(df["close"], length=5)
    df["ema12"] = ta.ema(df["close"], length=12)
    df["rsi"] = ta.rsi(df["close"], length=14)
    score = 0; notes = []
    if df["ema5"].iloc[-1] > df["ema12"].iloc[-1]:
        score += 0.5; notes.append("EMA5 > EMA12")
    else:
        score -= 0.5; notes.append("EMA5 < EMA12")
    r = df["rsi"].iloc[-1]
    notes.append(f"RSI={r:.1f}")
    if r > 70:
        score -= 0.3; notes.append("RSI перекуплен")
    elif r < 30:
        score += 0.3; notes.append("RSI перепродан")
    return score, notes

# ================= Команды =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup(PAIR_BUTTONS, resize_keyboard=True)
    await update.message.reply_text("👋 Привет! Выбери валютную пару:", reply_markup=kb)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - начать\n/auto on|off - включить/выключить автоанализ")

async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_running, ws_task
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Используй /auto on или /auto off")
        return
    arg = context.args[0].lower()
    if arg == "on":
        if auto_running:
            await update.message.reply_text("🔁 Уже включен.")
            return
        auto_running = True
        ws_task = asyncio.create_task(ws_worker(context.application, chat_id))
        await update.message.reply_text("🔁 Автоанализ включен.")
    elif arg == "off":
        auto_running = False
        if ws_task:
            ws_task.cancel()
            ws_task = None
        await update.message.reply_text("⏸ Автоанализ выключен.")
    else:
        await update.message.reply_text("Используй /auto on или /auto off")

# ================= Обработка пользовательского ввода =================
async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Если пара не выбрана
    if chat_id not in user_state:
        if text in PAIRS:
            user_state[chat_id] = {"pair": text}
            kb = ReplyKeyboardMarkup(TIME_BUTTONS, resize_keyboard=True)
            await update.message.reply_text(f"Пара: {text}\nВыбери время:", reply_markup=kb)
        else:
            await update.message.reply_text("Выбери валютную пару:")
        return

    # Если пара выбрана, выбираем время
    if "pair" in user_state[chat_id] and "time" not in user_state[chat_id]:
        if text in TIMES:
            user_state[chat_id]["time"] = text
            pair = user_state[chat_id]["pair"]
            data = [p for (_, p) in prices[pair]]
            if not data:
                await update.message.reply_text("Нет данных, включи /auto on")
                return
            score, notes = compute_score(data)
            direction = "🟩 Вверх" if score > 0 else "🟥 Вниз" if score < 0 else "⬜ Нейтрально"
            msg = f"🔔 Сигнал (по запросу)\nПара: {pair}\nНаправление: {direction}\n\n" + "\n".join(notes)
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("Выбери время из кнопок")

# ================= WebSocket =================
async def ws_worker(app, chat_id):
    global auto_running
    url = f"wss://ws.twelvedata.com/v1/quotes?apikey={TWELVE_API_KEY}"
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"action": "subscribe", "params": {"symbols": ",".join(PAIRS)}}))
            while auto_running:
                msg = await ws.recv()
                data = json.loads(msg)
                logger.info(f"WS update: {data}")  # Лог для отладки
                if "symbol" in data and "price" in data:
                    s, p = data["symbol"], float(data["price"])
                    prices[s].append((time.time(), p))
                    await check_signal(app, s, chat_id)
    except Exception as e:
        logger.error(f"WS error: {e}")
    finally:
        logger.info("WS закрыт")

async def check_signal(app, symbol, chat_id):
    now = time.time()
    if now - last_sent.get(symbol, 0) < COOLDOWN:
        return
    data = [p for (_, p) in prices[symbol]]
    score, notes = compute_score(data)
    if abs(score) >= SIGNAL_THRESHOLD:
        direction = "🟩 BUY" if score > 0 else "🟥 SELL"
        msg = f"🔔 Автосигнал\nПара: {symbol}\n{direction}\n\n" + "\n".join(notes)
        await app.bot.send_message(chat_id=chat_id, text=msg)
        last_sent[symbol] = now

# ================= Main =================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_input))
    logger.info("Bot запущен")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()  # Polling вместо webhook на Render
    await asyncio.Event().wait()  # держим бота живым

if __name__ == "__main__":
    asyncio.run(main())
