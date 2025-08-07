import os
import sys
import time
import logging
import threading
from decimal import Decimal, ROUND_DOWN

import pandas as pd
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from flask import Flask

# ======================================================================================================================
# CONFIGURATION
# ======================================================================================================================

# Load from environment variables
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")

# Trading parameters from environment variables
TRADE_QUANTITY_PERCENT = float(os.getenv("TRADE_QUANTITY_PERCENT", "10")) # Percentage of quote currency to use
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = int(os.getenv("RSI_OVERSOLD", "30"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_STD_DEV = int(os.getenv("BB_STD_DEV", "2"))
KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "1m")
TICK_INTERVAL_SECONDS = int(os.getenv("TICK_INTERVAL_SECONDS", "60"))

# ======================================================================================================================
# SETUP
# =================================================S=====================================================================

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout  # Log to standard output, which Railway captures
)

# --- State Management ---
# This is crucial to prevent the bot from buying/selling repeatedly.
# In a real-world scenario, you might persist this state in a database.
in_position = False

# --- Flask App for Health Check ---
# Railway requires a web process to be exposed. This simple Flask app serves that purpose.
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Trading Bot Worker is Alive"

# --- Binance Client ---
try:
    client = Client(API_KEY, API_SECRET)
    # Test connectivity
    client.ping()
    logging.info("Successfully connected to Binance API.")
except Exception as e:
    logging.error(f"Failed to connect to Binance API: {e}")
    sys.exit(1) # Exit if we can't connect

# --- Symbol Info & Filters ---
# Fetching these dynamically makes the bot adaptable to any symbol.
try:
    symbol_info = client.get_symbol_info(SYMBOL)
    BASE_ASSET = symbol_info['baseAsset']
    QUOTE_ASSET = symbol_info['quoteAsset']
    
    # Find the LOT_SIZE filter to determine quantity precision
    lot_size_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
    step_size = Decimal(lot_size_filter['stepSize'])
    quantity_precision = int(-step_size.log10())

    # Find the MIN_NOTIONAL filter
    min_notional_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL')
    MIN_NOTIONAL_VALUE = Decimal(min_notional_filter['minNotional'])

    logging.info(f"Successfully fetched symbol info for {SYMBOL}")
    logging.info(f"Base Asset: {BASE_ASSET}, Quote Asset: {QUOTE_ASSET}")
    logging.info(f"Quantity Precision (stepSize): {step_size} ({quantity_precision} decimal places)")
    logging.info(f"Minimum Notional Value: {MIN_NOTIONAL_VALUE}")

except Exception as e:
    logging.error(f"Could not get symbol info for {SYMBOL}. Error: {e}")
    sys.exit(1)

# ======================================================================================================================
# TECHNICAL INDICATOR FUNCTIONS
# ======================================================================================================================

def get_klines():
    try:
        data = client.get_klines(symbol=SYMBOL, interval=KLINE_INTERVAL, limit=100)
        df = pd.DataFrame(data, columns=["time", "o", "h", "l", "c", "v", "ct", "qv", "nt", "tbv", "tqv", "ignore"])
        df["c"] = pd.to_numeric(df["c"])
        return df
    except Exception as e:
        logging.error(f"Error fetching k-lines: {e}")
        return None

def calculate_indicators(df):
    # RSI
    delta = df["c"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema_fast = df["c"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = df["c"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()

    # Bollinger Bands
    middle_band = df["c"].rolling(window=BB_PERIOD).mean()
    std = df["c"].rolling(window=BB_PERIOD).std()
    df["bb_upper"] = middle_band + BB_STD_DEV * std
    df["bb_lower"] = middle_band - BB_STD_DEV * std
    
    return df

# ======================================================================================================================
# TRADING LOGIC
# ======================================================================================================================

def should_buy(df):
    latest = df.iloc[-1]
    return (
        latest["rsi"] < RSI_OVERSOLD and
        latest["macd"] > latest["signal"] and
        latest["c"] < latest["bb_lower"]
    )

def should_sell(df):
    latest = df.iloc[-1]
    return (
        latest["rsi"] > RSI_OVERBOUGHT and
        latest["macd"] < latest["signal"] and
        latest["c"] > latest["bb_upper"]
    )

def get_balance(asset):
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance['free'])
    except Exception as e:
        logging.error(f"Error getting balance for {asset}: {e}")
        return 0.0

def format_quantity(quantity):
    """Formats the quantity according to the symbol's stepSize."""
    return float(Decimal(quantity).quantize(step_size, rounding=ROUND_DOWN))

def trade():
    global in_position
    
    df = get_klines()
    if df is None or df.empty:
        logging.warning("Could not fetch k-lines. Skipping this tick.")
        return

    df = calculate_indicators(df)
    latest_price = df["c"].iloc[-1]

    logging.info(f"Checking {SYMBOL}... Price: {latest_price:.4f}, RSI: {df.iloc[-1]['rsi']:.2f}, In Position: {in_position}")

    # --- BUY LOGIC ---
    if not in_position and should_buy(df):
        quote_balance = get_balance(QUOTE_ASSET)
        trade_amount_quote = (quote_balance * TRADE_QUANTITY_PERCENT) / 100
        
        if trade_amount_quote < float(MIN_NOTIONAL_VALUE):
            logging.warning(f"BUY signal detected, but trade amount {trade_amount_quote:.4f} {QUOTE_ASSET} is below minimum notional of {MIN_NOTIONAL_VALUE}. Skipping buy.")
            return

        quantity_to_buy = format_quantity(trade_amount_quote / latest_price)
        logging.info(f"BUY SIGNAL! Attempting to buy {quantity_to_buy} {BASE_ASSET}...")
        
        try:
            order = client.order_market_buy(symbol=SYMBOL, quantity=quantity_to_buy)
            logging.info(f"✅ SUCCESS: Bought {order['executedQty']} {BASE_ASSET} at ~{latest_price}")
            in_position = True
        except BinanceAPIException as e:
            logging.error(f"BUY API ERROR: {e}")
        except Exception as e:
            logging.error(f"BUY FAILED: An unexpected error occurred: {e}")

    # --- SELL LOGIC ---
    elif in_position and should_sell(df):
        base_balance = get_balance(BASE_ASSET)
        quantity_to_sell = format_quantity(base_balance)

        if quantity_to_sell * latest_price < float(MIN_NOTIONAL_VALUE):
            logging.warning(f"SELL signal detected, but sell amount is below minimum notional. Balance: {base_balance} {BASE_ASSET}. Skipping sell.")
            return

        logging.info(f"SELL SIGNAL! Attempting to sell {quantity_to_sell} {BASE_ASSET}...")
        
        try:
            order = client.order_market_sell(symbol=SYMBOL, quantity=quantity_to_sell)
            logging.info(f"🚨 SUCCESS: Sold {order['executedQty']} {BASE_ASSET} at ~{latest_price}")
            in_position = False
        except BinanceAPIException as e:
            logging.error(f"SELL API ERROR: {e}")
        except Exception as e:
            logging.error(f"SELL FAILED: An unexpected error occurred: {e}")

# ======================================================================================================================
# MAIN EXECUTION
# ======================================================================================================================

def run_bot():
    """The main loop for the trading bot worker."""
    logging.info("Trading bot worker started.")
    while True:
        try:
            trade()
        except Exception as e:
            logging.error(f"An error occurred in the main trading loop: {e}")
        time.sleep(TICK_INTERVAL_SECONDS)

if __name__ == "__main__":
    # The 'worker' process will run this function in a background thread
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # The 'web' process will run the Flask app
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
