from flask import Flask
import os
import threading
import time
import logging
import numpy as np
import pandas as pd
from binance.client import Client
from binance.enums import *

# Load API keys from environment variables
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")  # Default: BTC/USDT

# Binance client
client = Client(API_KEY, API_SECRET)

# Flask app
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Trading bot is live!"

# Setup logging
logging.basicConfig(filename="trading_bot.log", level=logging.INFO, format="%(asctime)s - %(message)s")

# Technical indicator functions
def get_klines(symbol, interval="1m", limit=100):
    data = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(data, columns=["time", "o", "h", "l", "c", "v", "ct", "qv", "nt", "tbv", "tqv", "ignore"])
    df["c"] = df["c"].astype(float)
    return df

def calculate_indicators(df):
    df["rsi"] = compute_rsi(df["c"], 14)
    df["macd"], df["signal"] = compute_macd(df["c"])
    df["bb_upper"], df["bb_middle"], df["bb_lower"] = compute_bollinger_bands(df["c"])
    return df

def compute_rsi(series, period):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_macd(series):
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal

def compute_bollinger_bands(series, period=20, std_dev=2):
    middle_band = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper_band = middle_band + std_dev * std
    lower_band = middle_band - std_dev * std
    return upper_band, middle_band, lower_band

# Buy/sell logic
def should_buy(df):
    latest = df.iloc[-1]
    return (
        latest["rsi"] < 30 and
        latest["macd"] > latest["signal"] and
        latest["c"] < latest["bb_lower"]
    )

def should_sell(df):
    latest = df.iloc[-1]
    return (
        latest["rsi"] > 70 and
        latest["macd"] < latest["signal"] and
        latest["c"] > latest["bb_upper"]
    )

def get_balance(asset):
    balances = client.get_account()["balances"]
    for b in balances:
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0

def trade():
    df = get_klines(SYMBOL)
    df = calculate_indicators(df)

    base = SYMBOL.replace("USDT", "")
    usdt_balance = get_balance("USDT")
    coin_balance = get_balance(base)
    price = df["c"].iloc[-1]

    if should_buy(df) and usdt_balance > 10:
        quantity = round(usdt_balance / price, 6)
        try:
            client.order_market_buy(symbol=SYMBOL, quantity=quantity)
            logging.info(f"✅ Bought {quantity} {base} at {price}")
        except Exception as e:
            logging.error(f"Buy error: {e}")

    elif should_sell(df) and coin_balance * price > 10:
        quantity = round(coin_balance, 6)
        try:
            client.order_market_sell(symbol=SYMBOL, quantity=quantity)
            logging.info(f"🚨 Sold {quantity} {base} at {price}")
        except Exception as e:
            logging.error(f"Sell error: {e}")
    else:
        logging.info("⏳ No trade conditions met.")

# Background thread
def run_bot():
    while True:
        try:
            trade()
        except Exception as e:
            logging.error(f"Loop error: {e}")
        time.sleep(60)

# Run everything
if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
