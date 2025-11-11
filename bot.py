import os
import asyncio
import json
import logging
import time
from collections import deque, defaultdict

import pandas as pd
import pandas_ta as ta
import websockets
from telegram import ReplyKeyboardMarkup, Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("binary_signal_bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY")
PORT = int(os.environ.get("PORT", 8000))
WEBHOOK_URL = f"https://you-bot-l2y9.onrender.com"  # замени на URL своего Render сервиса

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

# ===== Индикаторы =====
def compute_score(series):
    if len(series) < 10:
        return 0, ["мало данных"]
    df = pd.DataFrame({"close": series})
    df["ema5"] = ta.ema(df["close"], length=5)
    df["ema12"] = ta.ema(df["close"], length=12)
    df["rsi"] = ta.rsi(df["close"], length=14)
    score = 0
    notes = []
    if df["ema5"].iloc[-1] > df["ema12"].iloc[-1]:
        score += 0.5
        notes.append("EMA5 > EMA12")
    else:
        score -= 0.5
        notes.append("EMA5 < EMA12")
    r = df["rsi"].iloc[-1]
    notes.append(f"RSI={r:.1f}")
    if r > 70:
        score -= 0.3
        notes.append("RSI перекуплен")
    elif r < 30:
        score += 0.3
        notes.append("RSI перепродан")
    return score, notes

# ===== Telegram команды =====
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

async def handle_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in PAIRS:
        return
    user_state[update.effective_chat.id] = text
    kb = ReplyKeyboardMarkup(TIME_BUTTONS, resize_keyboard=True)
    await update.message.reply_text(f"Пара: {text}\nВыбери время:", reply_markup=kb)

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in TIMES:
        return
    pair = user_state.get(update.effective_chat.id)
    if not pair:
        await update.message.reply_text("Сначала выбери пару.")
        return
    data = [p for (_, p) in prices[pair]]
    if not data:
        await update.message.reply_text("Нет данных, включи /auto on")
        return
    score, notes = compute_score(data)
    direction = "🟩 Вверх" if score > 0 else "🟥 Вниз" if score < 0 else "⬜ Нейтрально"
    msg = f"🔔 Сигнал (по запросу)\nПара: {pair}\nНаправление: {direction}\n\n" + "\n".join(notes)
    await update.message.reply_text(msg)

# ===== WebSocket обработка =====
async def ws_worker(app, chat_id):
    global auto_running
    url = f"wss://ws.twelvedata.com/v1/quotes?apikey={TWELVE_API_KEY}"
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"action": "subscribe", "params": {"symbols": ",".join(PAIRS)}}))
            while auto_running:
                msg = await ws.recv()
                data = json.loads(msg)
                if "symbol" in data and "price" in data:
                    s, p = data["symbol"], float(data["price"])
                    prices[s].append((time.time(), p))
                    await check_signal(app, s, chat_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"WS error: {e}")
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

# ===== Main =====
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Удаляем старый webhook перед стартом
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pair))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time))

    logger.info("Запуск webhook на Render")
    await app.start()
    await app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )
    await app.updater.idle()  # держит бота живым

if __name__ == "__main__":
    asyncio.run(main())
