import os
import sys
import time
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone

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

# A comma-separated list of symbols to monitor. Now a fallback, as we dynamically fetch top coins.
MONITOR_SYMBOLS_RAW = os.getenv("MONITOR_SYMBOLS", "") 

# Trading parameters from environment variables
# Percentage of quote currency to use for each trade.
TRADE_QUANTITY_PERCENT = float(os.getenv("TRADE_QUANTITY_PERCENT", "100")) 
# Cooldown period in seconds after a trade to prevent rapid-fire buy/sell cycles.
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS", "120"))

# Technical Indicator Parameters
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = int(os.getenv("RSI_OVERSOLD", "30"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_STD_DEV = int(os.getenv("BB_STD_DEV", "2"))
KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "1m")
# The bot will now check for trades every 30 seconds.
TICK_INTERVAL_SECONDS = int(os.getenv("TICK_INTERVAL_SECONDS", "30"))

# === NEW: Custom trading parameters for more proactive trading and risk management ===
# A more flexible RSI buy threshold to trade more frequently.
RSI_BUY_THRESHOLD = int(os.getenv("RSI_BUY_THRESHOLD", "50"))
# Stop-loss and take-profit percentages (expressed as 5.0 for 5%)
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "2.0"))
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", "1.0"))


# ======================================================================================================================
# SETUP
# ======================================================================================================================

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout  # Log to standard output, which Railway captures
)

# --- Flask App for Health Check ---
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Trading Bot Worker is Alive"

class TradingBot:
    """
    Encapsulates the trading bot's logic and state.
    """
    def __init__(self, api_key, api_secret):
        """Initializes the Binance client and bot state."""
        try:
            self.client = Client(api_key, api_secret)
            self.client.ping()
            logging.info("Successfully connected to Binance API.")
        except Exception as e:
            logging.error(f"Failed to connect to Binance API: {e}")
            sys.exit(1)
        
        # State management for multiple symbols
        self.in_position = {}
        self.symbol_info_cache = {}
        self.last_trade_time = {}

        self.monitor_symbols = self._initialize_symbols()

    def _get_top_symbols(self, limit=20):
        """Fetches the top symbols by trading volume and filters for USDT/FDUSD pairs."""
        try:
            tickers = self.client.get_ticker()
            usdt_fdusd_pairs = [t for t in tickers if t['symbol'].endswith('USDT') or t['symbol'].endswith('FDUSD')]
            sorted_pairs = sorted(usdt_fdusd_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
            top_symbols = [pair['symbol'] for pair in sorted_pairs[:limit]]
            logging.info(f"Dynamically fetched top {limit} symbols by trading volume.")
            return top_symbols
        except Exception as e:
            logging.error(f"Failed to fetch top symbols: {e}")
            return []

    def _find_emerging_symbols(self, time_limit_hours=24):
        """Finds newly listed coins to detect new trading opportunities."""
        emerging_symbols = []
        try:
            exchange_info = self.client.get_exchange_info()
            now = datetime.now(timezone.utc)
            for symbol_info in exchange_info['symbols']:
                list_time = datetime.fromtimestamp(symbol_info['listTime'] / 1000, tz=timezone.utc)
                if (symbol_info['symbol'].endswith('USDT') or symbol_info['symbol'].endswith('FDUSD')) and (now - list_time) < timedelta(hours=time_limit_hours):
                    emerging_symbols.append(symbol_info['symbol'])
            if emerging_symbols:
                logging.info(f"Found {len(emerging_symbols)} emerging symbols listed in the last {time_limit_hours} hours: {emerging_symbols}")
            return emerging_symbols
        except Exception as e:
            logging.error(f"Failed to find emerging symbols: {e}")
            return []

    def _initialize_symbols(self):
        """Combines dynamic and static symbol lists and caches their info."""
        monitor_symbols = list(set(self._get_top_symbols() + self._find_emerging_symbols()))
        if not monitor_symbols and MONITOR_SYMBOLS_RAW:
            monitor_symbols = [s.strip().upper() for s in MONITOR_SYMBOLS_RAW.split(',') if s.strip()]

        if not monitor_symbols:
            logging.error("No symbols to monitor. Exiting.")
            sys.exit(1)

        valid_symbols = []
        for symbol in monitor_symbols:
            try:
                symbol_info = self.client.get_symbol_info(symbol)
                if not symbol_info: raise ValueError(f"Symbol '{symbol}' not found.")
                
                lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
                min_notional_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)

                self.symbol_info_cache[symbol] = {
                    'BASE_ASSET': symbol_info['baseAsset'],
                    'QUOTE_ASSET': symbol_info['quoteAsset'],
                    'step_size': Decimal(lot_size_filter['stepSize']) if lot_size_filter else Decimal("0.00001"),
                    'MIN_NOTIONAL_VALUE': Decimal(min_notional_filter['minNotional']) if min_notional_filter else Decimal("10")
                }
                self.in_position[symbol] = {'status': False, 'buy_price': 0}
                self.last_trade_time[symbol] = datetime.now() - timedelta(seconds=TRADE_COOLDOWN_SECONDS)
                valid_symbols.append(symbol)
                logging.info(f"Successfully initialized symbol: {symbol}")
            except Exception as e:
                logging.error(f"Error initializing {symbol}: {e}. This symbol will be ignored.")

        if not valid_symbols:
            logging.error("No valid symbols could be initialized. Exiting.")
            sys.exit(1)
        
        logging.info(f"Monitoring the following symbols: {valid_symbols}")
        return valid_symbols

    def show_wallet_balance(self):
        """Fetches and logs the user's balances."""
        try:
            account_info = self.client.get_account()
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

    def get_balance(self, asset):
        """
        FIXED: Gets the available balance for a specific asset by checking the full account balance,
        which is more reliable than the individual asset balance endpoint.
        """
        try:
            account_info = self.client.get_account()
            for balance in account_info['balances']:
                if balance['asset'] == asset:
                    return float(balance['free'])
            return 0.0
        except Exception as e:
            logging.error(f"Error getting balance for {asset}: {e}")
            return 0.0

    def get_klines(self, symbol):
        """Fetches k-line data for a given symbol."""
        try:
            data = self.client.get_klines(symbol=symbol, interval=KLINE_INTERVAL, limit=100)
            df = pd.DataFrame(data, columns=["time", "o", "h", "l", "c", "v", "ct", "qv", "nt", "tbv", "tqv", "ignore"])
            df["c"] = pd.to_numeric(df["c"])
            return df
        except Exception as e:
            logging.error(f"Error fetching k-lines for {symbol}: {e}")
            return None

    def _calculate_indicators(self, df):
        """Calculates all technical indicators for the dataframe."""
        delta = df["c"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))

        ema_fast = df["c"].ewm(span=MACD_FAST, adjust=False).mean()
        ema_slow = df["c"].ewm(span=MACD_SLOW, adjust=False).mean()
        df["macd"] = ema_fast - ema_slow
        df["signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()

        middle_band = df["c"].rolling(window=BB_PERIOD).mean()
        std = df["c"].rolling(window=BB_PERIOD).std()
        df["bb_upper"] = middle_band + BB_STD_DEV * std
        df["bb_lower"] = middle_band - BB_STD_DEV * std
        return df

    def _should_buy(self, df):
        """Checks for a buy signal based on technical indicators."""
        latest = df.iloc[-1]
        return (latest["rsi"] < RSI_BUY_THRESHOLD and latest["macd"] > latest["signal"])

    def _should_sell(self, df, current_price, buy_price):
        """
        Checks for a sell signal based on stop-loss, take-profit, or technical indicators.
        """
        latest = df.iloc[-1]
        
        # Check for stop-loss
        price_drop_percent = ((buy_price - current_price) / buy_price) * 100
        if price_drop_percent >= STOP_LOSS_PERCENT:
            return "Stop-Loss Triggered"

        # Check for take-profit
        price_gain_percent = ((current_price - buy_price) / buy_price) * 100
        if price_gain_percent >= TAKE_PROFIT_PERCENT:
            return "Take-Profit Triggered"

        # Check for technical sell signal
        if (latest["rsi"] > RSI_OVERBOUGHT and latest["macd"] < latest["signal"]):
            return "Technical Signal"

        return None

    def _format_quantity(self, quantity, symbol):
        """Formats the quantity according to the symbol's stepSize."""
        step_size = self.symbol_info_cache[symbol]['step_size']
        return float(Decimal(quantity).quantize(step_size, rounding=ROUND_DOWN))

    def trade(self, symbol):
        """Performs the trading logic for a single symbol."""
        # Enforce cooldown period to avoid rapid trades
        if (datetime.now() - self.last_trade_time[symbol]).total_seconds() < TRADE_COOLDOWN_SECONDS:
            return

        df = self.get_klines(symbol)
        if df is None or df.empty:
            logging.warning(f"Could not fetch k-lines for {symbol}. Skipping this tick.")
            return

        df = self._calculate_indicators(df)
        latest_price = df["c"].iloc[-1]
        info = self.symbol_info_cache[symbol]
        base_asset = info['BASE_ASSET']
        quote_asset = info['QUOTE_ASSET']
        min_notional_value = info['MIN_NOTIONAL_VALUE']

        logging.info(f"Checking {symbol}... Price: {latest_price:.4f}, RSI: {df.iloc[-1]['rsi']:.2f}, In Position: {self.in_position[symbol]['status']}")

        # --- BUY LOGIC ---
        if not self.in_position[symbol]['status'] and self._should_buy(df):
            quote_balance = self.get_balance(quote_asset)
            trade_amount_quote = (quote_balance * TRADE_QUANTITY_PERCENT) / 100
            
            if trade_amount_quote < float(min_notional_value):
                logging.warning(f"BUY signal detected for {symbol}, but trade amount {trade_amount_quote:.4f} {quote_asset} is below minimum notional of {min_notional_value}. Skipping buy.")
                return

            quantity_to_buy = self._format_quantity(trade_amount_quote / latest_price, symbol)
            logging.info(f"BUY SIGNAL for {symbol}! Attempting to buy {quantity_to_buy} {base_asset} with {trade_amount_quote:.2f} {quote_asset}...")
            
            try:
                order = self.client.order_market_buy(symbol=symbol, quantity=quantity_to_buy)
                self.in_position[symbol]['status'] = True
                self.in_position[symbol]['buy_price'] = latest_price
                self.last_trade_time[symbol] = datetime.now()
                logging.info(f"✅ SUCCESS: Bought {order['executedQty']} {base_asset} for {symbol} at ~{latest_price}. Recording buy price for risk management.")
            except BinanceAPIException as e:
                logging.error(f"BUY API ERROR for {symbol}: {e}")
            except Exception as e:
                logging.error(f"BUY FAILED for {symbol}: An unexpected error occurred: {e}")

        # --- SELL LOGIC ---
        elif self.in_position[symbol]['status']:
            buy_price = self.in_position[symbol]['buy_price']
            sell_signal_reason = self._should_sell(df, latest_price, buy_price)
            
            if sell_signal_reason:
                base_balance = self.get_balance(base_asset)
                quantity_to_sell = self._format_quantity(base_balance, symbol)

                if quantity_to_sell * latest_price < float(min_notional_value):
                    logging.warning(f"SELL signal ({sell_signal_reason}) detected for {symbol}, but sell amount is below minimum notional. Skipping sell.")
                    return

                logging.info(f"SELL SIGNAL for {symbol} ({sell_signal_reason})! Attempting to sell {quantity_to_sell} {base_asset}...")
                
                try:
                    order = self.client.order_market_sell(symbol=symbol, quantity=quantity_to_sell)
                    logging.info(f"🚨 SUCCESS: Sold {order['executedQty']} {base_asset} for {symbol} at ~{latest_price}")
                    self.in_position[symbol]['status'] = False
                    self.in_position[symbol]['buy_price'] = 0
                    self.last_trade_time[symbol] = datetime.now()
                except BinanceAPIException as e:
                    logging.error(f"SELL API ERROR for {symbol}: {e}")
                except Exception as e:
                    logging.error(f"SELL FAILED for {symbol}: An unexpected error occurred: {e}")

    def run(self):
        """The main loop for the trading bot worker."""
        logging.info("Trading bot worker started.")
        self.show_wallet_balance()
        while True:
            try:
                for symbol in self.monitor_symbols:
                    self.trade(symbol)
            except Exception as e:
                logging.error(f"An error occurred in the main trading loop: {e}")
            time.sleep(TICK_INTERVAL_SECONDS)

# ======================================================================================================================
# MAIN EXECUTION
# ======================================================================================================================

if __name__ == "__main__":
    bot = TradingBot(API_KEY, API_SECRET)
    bot_thread = threading.Thread(target=bot.run)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
