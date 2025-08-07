from flask import Flask
import os
import time
import logging
from threading import Thread
from binance.client import Client

# Setup logging
logging.basicConfig(filename="trading_bot.log", level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Flask App
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Trading bot is live!"

@app.route("/status")
def status():
    return "✅ Bot is running OK!"

# Binance Client Setup (from environment)
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
client = Client(API_KEY, API_SECRET)

# Trading Bot Logic
def run_trading_bot():
    while True:
        try:
            logging.info("📈 Bot tick...")
            balance = client.get_asset_balance(asset='USDT')
            logging.info(f"USDT Balance: {balance['free']}")
            time.sleep(60)
        except Exception as e:
            logging.error(f"Error in bot: {e}")
            time.sleep(60)

# Flask run in thread
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)

# MAIN ENTRY
if __name__ == "__main__":
    Thread(target=run_flask).start()
    run_trading_bot()
