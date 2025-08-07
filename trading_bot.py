import os
import time
import logging
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, jsonify
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# === Setup Logging ===
logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# === Load API credentials from environment ===
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

if not API_KEY or not API_SECRET:
    raise Exception("Please set BINANCE_API_KEY and BINANCE_API_SECRET environment variables.")

client = Client(API_KEY, API_SECRET)

# === Flask for Monitoring ===
app = Flask(__name__)
status = {
    "running": True,
    "last_prices": {},
    "last_action": {},
    "last_trade_time": {},
}

@app.route("/status")
def get_status():
    return jsonify(status)

def run_flask():
    app.run(port=5000)

# === Configurable Constants ===
TRADE_AMOUNT_USDT = 15
MIN_VOLUME = 1_000_000  # Minimum 24h quote volume
TRADE_COOLDOWN_SECONDS = 60

# === Helper Functions ===
def get_top_symbols(limit=10):
    tickers = client.get_ticker_24hr()
    sorted_tickers = sorted(
        [t for t in tickers if t['symbol'].endswith('USDT') and
         not t['symbol'].endswith('BUSDUSDT') and
         float(t['quoteVolume']) > MIN_VOLUME],
        key=lambda x: float(x['quoteVolume']),
        reverse=True
    )
    return [t['symbol'] for t in sorted_tickers[:limit]]

def get_price(symbol):
    ticker = client.get_symbol_ticker(symbol=symbol)
    return float(ticker['price'])

def has_sufficient_balance(usdt_amount):
    balance = float(client.get_asset_balance(asset='USDT')['free'])
    return balance >= usdt_amount

def can_trade(symbol):
    last_trade = status["last_trade_time"].get(symbol)
    if not last_trade:
        return True
    return datetime.now() - last_trade > timedelta(seconds=TRADE_COOLDOWN_SECONDS)

def place_order(symbol, side, usdt_amount):
    price = get_price(symbol)
    quantity = round(usdt_amount / price, 5)
    if not has_sufficient_balance(usdt_amount):
        logging.warning(f"Not enough USDT to trade {symbol}. Needed: {usdt_amount}")
        return None
    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=quantity
        )
        logging.info(f"{side} order placed for {symbol}: {order}")
        status["last_action"][symbol] = f"{side} @ {price}"
        status["last_trade_time"][symbol] = datetime.now()
        return order
    except BinanceAPIException as e:
        logging.error(f"Error placing order for {symbol}: {e}")
        return None

# === Trading Logic ===
def simple_strategy():
    symbols = get_top_symbols()
    last_prices = {symbol: get_price(symbol) for symbol in symbols}
    status["last_prices"] = last_prices.copy()

    logging.info("Started trading loop with symbols: " + ", ".join(symbols))
    
    while True:
        time.sleep(10)
        for symbol in symbols:
            try:
                current_price = get_price(symbol)
                last_price = last_prices[symbol]
                change = (current_price - last_price) / last_price

                logging.info(f"{symbol}: price={current_price:.4f}, change={change:.2%}")
                
                if change <= -0.01 and can_trade(symbol):
                    logging.info(f"{symbol}: Buy signal")
                    place_order(symbol, SIDE_BUY, TRADE_AMOUNT_USDT)
                    last_prices[symbol] = current_price
                    status["last_prices"][symbol] = current_price

                elif change >= 0.01 and can_trade(symbol):
                    logging.info(f"{symbol}: Sell signal")
                    place_order(symbol, SIDE_SELL, TRADE_AMOUNT_USDT)
                    last_prices[symbol] = current_price
                    status["last_prices"][symbol] = current_price

            except Exception as e:
                logging.error(f"Error processing {symbol}: {e}")

# === Run Flask + Bot in Threads ===
if __name__ == "__main__":
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    try:
        simple_strategy()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
