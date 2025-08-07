import os
import sys
import time
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta

import pandas as pd
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException, BinanceRequestException, BinanceAPIException
from flask import Flask

# ======================================================================================================================
# CONFIGURATION
# ======================================================================================================================

# Load from environment variables
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# A comma-separated list of symbols to monitor. Now a fallback, as we will dynamically fetch top coins.
MONITOR_SYMBOLS_RAW = os.getenv("MONITOR_SYMBOLS", "") 

# Trading parameters from environment variables
# Updated default to 40% as requested.
TRADE_QUANTITY_PERCENT = float(os.getenv("TRADE_QUANTITY_PERCENT", "40")) # Percentage of quote currency to use
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
# ======================================================================================================================

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout  # Log to standard output, which Railway captures
)

# --- State Management ---
# in_position is now a dictionary to track position for multiple symbols.
# symbol_info_cache stores filter info to avoid repeated API calls.
in_position = {}
symbol_info_cache = {}

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

# --- Symbol Initialization ---

def get_top_symbols(limit=20):
    """
    Fetches the top symbols by trading volume and filters for USDT pairs.
    """
    try:
        # Get all 24hr tickers
        tickers = client.get_ticker()
        
        # Filter for USDT pairs and sort by quote volume (in descending order)
        usdt_pairs = [t for t in tickers if t['symbol'].endswith('USDT')]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        
        # Return the top `limit` symbols
        top_symbols = [pair['symbol'] for pair in sorted_pairs[:limit]]
        logging.info(f"Dynamically fetched top {limit} symbols by trading volume.")
        return top_symbols
    except Exception as e:
        logging.error(f"Failed to fetch top symbols: {e}")
        return []
        
def find_emerging_symbols(time_limit_hours=24):
    """
    Finds newly listed coins by checking the 'listTime' property.
    This helps the bot detect new trading opportunities.
    """
    emerging_symbols = []
    try:
        exchange_info = client.get_exchange_info()
        now = datetime.utcnow()
        for symbol_info in exchange_info['symbols']:
            # The listTime is in milliseconds, so convert to seconds
            list_time = datetime.utcfromtimestamp(symbol_info['listTime'] / 1000)
            # Check if the symbol is a USDT pair and was listed within the time limit
            if symbol_info['symbol'].endswith('USDT') and (now - list_time) < timedelta(hours=time_limit_hours):
                emerging_symbols.append(symbol_info['symbol'])
        
        if emerging_symbols:
            logging.info(f"Found {len(emerging_symbols)} emerging symbols listed in the last {time_limit_hours} hours: {emerging_symbols}")
        
        return emerging_symbols
    except Exception as e:
        logging.error(f"Failed to find emerging symbols: {e}")
        return []

# The bot will now prioritize the dynamically fetched symbols.
MONITOR_SYMBOLS = list(set(get_top_symbols() + find_emerging_symbols()))

# If the dynamic fetch fails, fall back to the environment variable list, if provided.
if not MONITOR_SYMBOLS and MONITOR_SYMBOLS_RAW:
    MONITOR_SYMBOLS = [s.strip().upper() for s in MONITOR_SYMBOLS_RAW.split(',') if s.strip()]

if not MONITOR_SYMBOLS:
    logging.error("No symbols could be fetched dynamically and no symbols specified in MONITOR_SYMBOLS. Exiting.")
    sys.exit(1)

# Initialize caches and filters for the selected symbols
valid_symbols = []
for symbol in MONITOR_SYMBOLS:
    try:
        symbol_info = client.get_symbol_info(symbol)
        if not symbol_info:
            raise ValueError(f"Symbol '{symbol}' not found on Binance.")

        # Use next() with a default value to prevent StopIteration if the filter is not found
        lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
        min_notional_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)

        # Store filter info in a cache
        symbol_info_cache[symbol] = {
            'BASE_ASSET': symbol_info['baseAsset'],
            'QUOTE_ASSET': symbol_info['quoteAsset'],
            'step_size': Decimal(lot_size_filter['stepSize']) if lot_size_filter else Decimal("0.00001"),
            'MIN_NOTIONAL_VALUE': Decimal(min_notional_filter['minNotional']) if min_notional_filter else Decimal("10")
        }
        in_position[symbol] = False
        valid_symbols.append(symbol)
        
        logging.info(f"Successfully initialized symbol: {symbol}")

    except BinanceAPIException as e:
        logging.error(f"Binance API Error for {symbol}: Code: {e.code}, Message: {e.message}. This symbol will be ignored.")
    except ValueError as e:
        logging.error(f"Configuration Error for {symbol}: {e}. This symbol will be ignored.")
    except Exception as e:
        logging.exception(f"An unexpected error occurred while initializing {symbol}. This symbol will be ignored.")

if not valid_symbols:
    logging.error("No valid symbols could be initialized. Exiting.")
    sys.exit(1)

MONITOR_SYMBOLS = valid_symbols
logging.info(f"Monitoring the following symbols: {MONITOR_SYMBOLS}")

# ======================================================================================================================
# WALLET AND BALANCE FUNCTIONS
# ======================================================================================================================

def show_wallet_balance():
    """Fetches and logs the user's balances for all coins with a non-zero balance."""
    try:
        account_info = client.get_account()
        balances = account_info['balances']
        logging.info("--- CURRENT WALLET BALANCE ---")
        for asset in balances:
            free = float(asset['free'])
            locked = float(asset['locked'])
            if free > 0 or locked > 0:
                logging.info(f"  {asset['asset']}: {free:.8f} (Free) / {locked:.8f} (Locked)")
        logging.info("------------------------------")
    except Exception as e:
        logging.error(f"Error fetching wallet balance: {e}")

def get_balance(asset):
    """Gets the available balance for a specific asset."""
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance['free'])
    except Exception as e:
        logging.error(f"Error getting balance for {asset}: {e}")
        return 0.0

# ======================================================================================================================
# TECHNICAL INDICATOR FUNCTIONS
# ======================================================================================================================

def get_klines(symbol):
    """Fetches k-line data for a given symbol."""
    try:
        data = client.get_klines(symbol=symbol, interval=KLINE_INTERVAL, limit=100)
        df = pd.DataFrame(data, columns=["time", "o", "h", "l", "c", "v", "ct", "qv", "nt", "tbv", "tqv", "ignore"])
        df["c"] = pd.to_numeric(df["c"])
        return df
    except Exception as e:
        logging.error(f"Error fetching k-lines for {symbol}: {e}")
        return None

def calculate_indicators(df):
    """Calculates all technical indicators for the dataframe."""
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

def format_quantity(quantity, symbol):
    """Formats the quantity according to the symbol's stepSize."""
    step_size = symbol_info_cache[symbol]['step_size']
    return float(Decimal(quantity).quantize(step_size, rounding=ROUND_DOWN))

def trade(symbol):
    """Performs the trading logic for a single symbol."""
    global in_position
    
    df = get_klines(symbol)
    if df is None or df.empty:
        logging.warning(f"Could not fetch k-lines for {symbol}. Skipping this tick.")
        return

    df = calculate_indicators(df)
    latest_price = df["c"].iloc[-1]
    
    # Get symbol-specific trading info from the cache
    info = symbol_info_cache[symbol]
    base_asset = info['BASE_ASSET']
    quote_asset = info['QUOTE_ASSET']
    min_notional_value = info['MIN_NOTIONAL_VALUE']

    logging.info(f"Checking {symbol}... Price: {latest_price:.4f}, RSI: {df.iloc[-1]['rsi']:.2f}, In Position: {in_position.get(symbol, False)}")

    # --- BUY LOGIC ---
    if not in_position.get(symbol, False) and should_buy(df):
        quote_balance = get_balance(quote_asset)
        trade_amount_quote = (quote_balance * TRADE_QUANTITY_PERCENT) / 100
        
        if trade_amount_quote < float(min_notional_value):
            logging.warning(f"BUY signal detected for {symbol}, but trade amount {trade_amount_quote:.4f} {quote_asset} is below minimum notional of {min_notional_value}. Skipping buy.")
            return

        quantity_to_buy = format_quantity(trade_amount_quote / latest_price, symbol)
        logging.info(f"BUY SIGNAL for {symbol}! Attempting to buy {quantity_to_buy} {base_asset}...")
        
        try:
            order = client.order_market_buy(symbol=symbol, quantity=quantity_to_buy)
            logging.info(f"✅ SUCCESS: Bought {order['executedQty']} {base_asset} for {symbol} at ~{latest_price}")
            in_position[symbol] = True
        except BinanceAPIException as e:
            logging.error(f"BUY API ERROR for {symbol}: {e}")
        except Exception as e:
            logging.error(f"BUY FAILED for {symbol}: An unexpected error occurred: {e}")

    # --- SELL LOGIC ---
    elif in_position.get(symbol, False) and should_sell(df):
        base_balance = get_balance(base_asset)
        quantity_to_sell = format_quantity(base_balance, symbol)

        if quantity_to_sell * latest_price < float(min_notional_value):
            logging.warning(f"SELL signal detected for {symbol}, but sell amount is below minimum notional. Balance: {base_balance} {base_asset}. Skipping sell.")
            return

        logging.info(f"SELL SIGNAL for {symbol}! Attempting to sell {quantity_to_sell} {base_asset}...")
        
        try:
            order = client.order_market_sell(symbol=symbol, quantity=quantity_to_sell)
            logging.info(f"🚨 SUCCESS: Sold {order['executedQty']} {base_asset} for {symbol} at ~{latest_price}")
            in_position[symbol] = False
        except BinanceAPIException as e:
            logging.error(f"SELL API ERROR for {symbol}: {e}")
        except Exception as e:
            logging.error(f"SELL FAILED for {symbol}: An unexpected error occurred: {e}")

# ======================================================================================================================
# MAIN EXECUTION
# ======================================================================================================================

def run_bot():
    """The main loop for the trading bot worker."""
    logging.info("Trading bot worker started.")
    show_wallet_balance() # Show balance once at startup
    while True:
        try:
            for symbol in MONITOR_SYMBOLS:
                trade(symbol)
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
