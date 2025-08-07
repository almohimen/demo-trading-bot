import os
import time
import logging
from threading import Thread
from datetime import datetime
from flask import Flask, jsonify
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# === Logging setup ===
logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# === Binance API keys ===
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

if not API_KEY or not API_SECRET:
    raise Exception("Missing Binance API credentials.")

client = Client(API_KEY, API_SECRET)

# === Flask setup ===
app = Flask(__name__)
status = {
    "running": True,
    "last_prices": {},
    "last_action": {},
    "last_trade_time": {}
}

@app.route("/")
def home():
    return "Trading bot is live!"

@app.route("/status")
def get_status():
    return jsonify(status)

# === Bot config ===
TRADE_AMOUNT_USDT = 15
MIN_VOLUME = 1_000_000
COOLDOWN = 60  # seconds cooldown between trades per symbol

def get_top_symbols(limit=10):
    tickers = client.get_ticker_24hr()
    sorted_tickers = sorted(
        [t for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > MIN_VOLUME],
        key=lambda x: float(x['quoteVolume']),
        reverse=True
    )
    return [t['symbol'] for t in sorted_tickers[:limit]]

def get_price(symbol):
    return float(client.get_symbol_ticker(symbol=symbol)['price'])

def has_usdt(amount):
    balance = float(client.get_asset_balance(asset='USDT')['free'])
    return balance >= amount

def can_trade(symbol):
    last_trade = status["last_trade_time"].get(symbol)
    return not last_trade or (datetime.now() - last_trade).total_seconds() > COOLDOWN

def place_order(symbol, side, usdt):
    price = get_price(symbol)
    qty = round(usdt / price, 5)
    if side == SIDE_BUY and not has_usdt(usdt):
        logging.warning(f"Insufficient USDT for {symbol}")
        return

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
        logging.info(f"{side} order placed for {symbol}: {order}")
        status["last_action"][symbol] = f"{side} @ {price}"
        status["last_trade_time"][symbol] = datetime.now()
    except BinanceAPIException as e:
        logging.error(f"Order failed for {symbol}: {e}")

def simple_strategy():
    logging.info(">>> simple_strategy() started <<<")
    symbols = get_top_symbols()
    last_prices = {s: get_price(s) for s in symbols}
    status["last_prices"] = last_prices.copy()

    while True:
        time.sleep(10)
        for symbol in symbols:
            try:
                price = get_price(symbol)
                change = (price - last_prices[symbol]) / last_prices[symbol]

                if change <= -0.01 and can_trade(symbol):
                    place_order(symbol, SIDE_BUY, TRADE_AMOUNT_USDT)
                    last_prices[symbol] = price
                    status["last_prices"][symbol] = price
                elif change >= 0.01 and can_trade(symbol):
                    place_order(symbol, SIDE_SELL, TRADE_AMOUNT_USDT)
                    last_prices[symbol] = price
                    status["last_prices"][symbol] = price
            except Exception as e:
                logging.error(f"Error with {symbol}: {e}")

if __name__ == "__main__":
    # Run Flask app in a background thread
    def run_flask():
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, threaded=True)

    Thread(target=run_flask, daemon=True).start()

    # Run trading bot in main thread
    try:
        simple_strategy()
    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")
    except Exception as e:
        logging.error(f"Fatal error: {e}")

