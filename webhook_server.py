import os
from dotenv import load_dotenv
import talib
import numpy as np
import requests
import threading
import time
import logging
from pybit.unified_trading import HTTP, WebSocket
from retry import retry
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
import asyncio
from flask import Flask, request, jsonify
import git
import shutil

# טעינת קובץ .env
load_dotenv()

# הגדרת לוגים
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# קריאת משתנים מ-.env
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LEVERAGE = int(os.getenv("LEVERAGE", 5))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 100))
RISK_REWARD_RATIO = float(os.getenv("RISK_REWARD_RATIO", 0.33))
NGROK_URL = os.getenv("NGROK_URL")
GITHUB_REPO_URL = "https://github.com/dhodeda/trading-bot.git"

# חיבור ל-Bybit
bybit = HTTP(testnet=False, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
ws = WebSocket(testnet=False, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, channel_type="linear")

# חיבור לטלגרם
app_telegram = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Flask עבור Webhook
app_flask = Flask(__name__)

# עדכון הקוד מ-GitHub
def update_code_from_github():
    try:
        repo_dir = "temp_repo"
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir)
        repo = git.Repo.clone_from(GITHUB_REPO_URL, repo_dir)
        with open(os.path.join(repo_dir, "webhook_server.py"), "r", encoding="utf-8") as f:
            new_code = f.read()
        with open("webhook_server.py", "w", encoding="utf-8") as f:
            f.write(new_code)
        logger.info("קוד עודכן בהצלחה מ-GitHub!")
        asyncio.run(send_telegram_alert("✅ הקוד עודכן בהצלחה מ-GitHub!"))
        os._exit(0)  # הפעלה מחדש של התוכנית
    except Exception as e:
        logger.error(f"שגיאה בעדכון הקוד מ-GitHub: {str(e)}")

# שליחת הודעה לטלגרם עם כפתורים
async def send_telegram_alert(message, buttons=None):
    try:
        if buttons:
            keyboard = InlineKeyboardMarkup(buttons)
            await app_telegram.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, reply_markup=keyboard, parse_mode='Markdown')
        else:
            await app_telegram.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"שליחת הודעה לטלגרם נכשלה: {str(e)}")

# חישוב אינדיקטורים
@retry(tries=3, delay=2)
def calculate_indicators(symbol):
    candles = bybit.get_kline(category="linear", symbol=symbol, interval="15m", limit=200)
    close_prices = np.array([float(c[4]) for c in candles["result"]["list"]])
    volumes = np.array([float(c[5]) for c in candles["result"]["list"]])

    ema9 = talib.EMA(close_prices, timeperiod=9)[-1]
    sma21 = talib.SMA(close_prices, timeperiod=21)[-1]
    rsi = talib.RSI(close_prices, timeperiod=14)[-1]
    macd, macdsignal, _ = talib.MACD(close_prices, fastperiod=12, slowperiod=26, signalperiod=9)
    vwap = np.sum(close_prices * volumes) / np.sum(volumes)
    volume_spike = volumes[-1] > np.mean(volumes[-10:]) * 1.5

    return {
        "EMA9": ema9,
        "SMA21": sma21,
        "RSI": rsi,
        "MACD": macd[-1],
        "MACD_Signal": macdsignal[-1],
        "VWAP": vwap,
        "VolumeSpike": volume_spike
    }

# חישוב גודל פוזיציה
def calculate_position_size(entry_price, symbol, risk_amount=RISK_PER_TRADE, leverage=LEVERAGE):
    try:
        account_balance = float(bybit.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["totalEquity"])
        risk_percentage = risk_amount / account_balance
        position_value = risk_amount * leverage
        position_size = position_value / entry_price
        return round(position_size, 3)
    except Exception as e:
        logger.error(f"שגיאה בחישוב גודל פוזיציה: {str(e)}")
        return 0.01

# חישוב SL/TP דינמי
def calculate_sl_tp(price, trade_type):
    volatility = bybit.get_kline(category="linear", symbol="BTCUSDT", interval="15m", limit=20)["result"]["list"]
    atr = talib.ATR(np.array([float(c[2]) for c in volatility]), 
                    np.array([float(c[3]) for c in volatility]), 
                    np.array([float(c[4]) for c in volatility]), timeperiod=14)[-1]
    
    if trade_type == "long":
        sl = price - (atr * (1 / RISK_REWARD_RATIO))
        tp = price + (atr * (1 / RISK_REWARD_RATIO) * 2)
    else:
        sl = price + (atr * (1 / RISK_REWARD_RATIO))
        tp = price - (atr * (1 / RISK_REWARD_RATIO) * 2)
    return sl, tp

# ניהול פוזיציות
def manage_existing_position(symbol, new_side):
    try:
        response = bybit.get_positions(category="linear", symbol=symbol)
        position = response["result"]["list"][0]
        size = float(position["size"])
        if size > 0:
            existing_side = position["side"]
            if existing_side == new_side:
                asyncio.run(send_telegram_alert(f"⚠️ קיימת כבר פוזיציית {existing_side} פתוחה על {symbol}"))
                return False
            else:
                close_side = "Sell" if existing_side == "Buy" else "Buy"
                bybit.place_order(category="linear", symbol=symbol, side=close_side, order_type="Market", qty=size)
                asyncio.run(send_telegram_alert(f"📢 סגירת פוזיציית {existing_side} על {symbol}"))
                time.sleep(1)
                return True
        return True
    except Exception as e:
        logger.info(f"אין פוזיציה קיימת עבור {symbol}: {str(e)}")
        return True

# ניתוח וביצוע עסקה
async def analyze_and_trade(symbol, price):
    try:
        indicators = calculate_indicators(symbol)
        if not manage_existing_position(symbol, "Buy" if indicators["EMA9"] > indicators["SMA21"] else "Sell"):
            return
        
        trade_type = "long" if indicators["EMA9"] > indicators["SMA21"] else "short"
        sl, tp = calculate_sl_tp(price, trade_type)
        position_size = calculate_position_size(price, symbol)

        if (indicators["EMA9"] > indicators["SMA21"] and 
            indicators["MACD"] > indicators["MACD_Signal"] and 
            indicators["RSI"] < 70 and 
            indicators["VolumeSpike"]):
            buttons = [[InlineKeyboardButton("בצע לונג", callback_data=f"trade:{symbol}:Buy:{price}:{sl}:{tp}:{position_size}")]]
            message = f"📈 הזדמנות לונג ב-{symbol}\nמחיר: {price}\nTP: {tp}\nSL: {sl}\nגודל: {position_size}"
            await send_telegram_alert(message, buttons)
        
        elif (indicators["EMA9"] < indicators["SMA21"] and 
              indicators["MACD"] < indicators["MACD_Signal"] and 
              indicators["RSI"] > 30 and 
              indicators["VolumeSpike"]):
            buttons = [[InlineKeyboardButton("בצע שורט", callback_data=f"trade:{symbol}:Sell:{price}:{sl}:{tp}:{position_size}")]]
            message = f"📉 הזדמנות שורט ב-{symbol}\nמחיר: {price}\nTP: {tp}\nSL: {sl}\nגודל: {position_size}"
            await send_telegram_alert(message, buttons)

    except Exception as e:
        logger.error(f"שגיאה בניתוח: {str(e)}")

# ביצוע עסקה
async def place_order(symbol, side, price, qty, sl, tp):
    try:
        order = bybit.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=qty,
            stopLoss=str(sl),
            takeProfit=str(tp),
            leverage=LEVERAGE
        )
        order_id = order["result"]["orderId"]
        message = f"📢 עסקה בוצעה!\n📊 {symbol}\n🔹 {side}\n💰 מחיר: {price}\n🎯 TP: {tp}\n🛑 SL: {sl}\n✅ ID: {order_id}"
        await send_telegram_alert(message)
    except Exception as e:
        logger.error(f"שגיאה בביצוע עסקה: {str(e)}")

# ניטור שוק
def monitor_market(symbol="BTCUSDT"):
    def handle_message(msg):
        try:
            symbol = msg["topic"].split(".")[1]
            price = float(msg["data"]["price"])
            asyncio.run(analyze_and_trade(symbol, price))
        except Exception as e:
            logger.error(f"שגיאה בעיבוד הודעה: {str(e)}")

    while True:
        try:
            ws.trade_stream(symbol=symbol, callback=handle_message)
        except Exception as e:
            logger.error(f"שגיאת WebSocket: {str(e)}. מתחבר מחדש...")
            time.sleep(5)

# טיפול בפקודות מטלגרם
async def handle_trade(update, context):
    query = update.callback_query
    data = query.data.split(":")
    if data[0] == "trade":
        symbol, side, price, sl, tp, qty = data[1], data[2], float(data[3]), float(data[4]), float(data[5]), float(data[6])
        await place_order(symbol, side, price, qty, sl, tp)
    await query.answer()

# Webhook של Flask
@app_flask.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "לא התקבל מידע"}), 400
        symbol = data.get("symbol", "BTCUSDT")
        side = data.get("side", "Buy")
        price = float(data.get("price", 0))
        sl, tp = calculate_sl_tp(price, "long" if side == "Buy" else "short")
        qty = calculate_position_size(price, symbol)
        asyncio.run(place_order(symbol, side, price, qty, sl, tp))
        return jsonify({"status": "success", "message": f"עסקה בוצעה עבור {symbol}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

# עדכון תקופתי
def periodic_update():
    while True:
        time.sleep(3600)  # בדיקה כל שעה
        update_code_from_github()

# הרצת הבוט
if __name__ == "__main__":
    app_telegram.add_handler(CallbackQueryHandler(handle_trade))
    threading.Thread(target=monitor_market, args=("BTCUSDT",), daemon=True).start()
    threading.Thread(target=lambda: app_flask.run(host='0.0.0.0', port=5000, debug=False), daemon=True).start()
    threading.Thread(target=periodic_update, daemon=True).start()
    app_telegram.run_polling()