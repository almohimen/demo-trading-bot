# **Aggressive Trading Bot**

This repository contains an aggressive, multi-quote asset trading bot written in Python for the Binance exchange. The bot is designed to run as a worker process on platforms like Railway and uses a combination of technical indicators to find trading opportunities.  
The bot's primary goal is to make frequent trades by using a proactive strategy based on RSI and MACD signals, while also protecting the portfolio with a tight stop-loss and take-profit mechanism.

## **Features**

* **Dynamic Symbol Monitoring**: The bot automatically fetches and monitors the top 20 symbols by volume, along with any newly listed coins, that are paired with USDT or FDUSD.  
* **Proactive Strategy**: It uses a less restrictive RSI and MACD crossover for buy signals, allowing it to enter trades more frequently.  
* **Risk Management**: Includes a take-profit and stop-loss system to secure gains and cut losses quickly.  
* **Full Balance Utilization**: The bot is configured to use 100% of the available quote asset balance for each trade, ensuring maximum capital efficiency.  
* **Containerized Deployment**: Built to run seamlessly on a platform like Railway, with a Flask health check endpoint.

## **Requirements**

To run this bot, you will need:

* A **Binance Account** with USDT or FDUSD in your Spot Wallet.  
* **API Keys** from your Binance account with permission to read your wallet and place orders.  
* A deployment environment (e.g., Railway, Heroku) configured with the necessary environment variables.

## **Setup & Configuration**

The bot is configured using environment variables. You must set the following variables in your deployment environment:

| Variable Name | Description | Example Value |
| :---- | :---- | :---- |
| API\_KEY | Your Binance API Key | abc... |
| API\_SECRET | Your Binance API Secret | xyz... |
| MONITOR\_SYMBOLS | (Optional) A comma-separated list of specific symbols to monitor. If empty, the bot will dynamically fetch top symbols. | BTCUSDT,ETHUSDT |
| TRADE\_QUANTITY\_PERCENT | The percentage of your quote asset balance to use for a trade. Default is 100\. | 100 |
| TICK\_INTERVAL\_SECONDS | How often the bot checks for trading signals. Default is 30\. | 30 |
| RSI\_BUY\_THRESHOLD | The RSI value below which a buy signal is considered. Default is 50\. | 50 |
| TAKE\_PROFIT\_PERCENT | The percentage gain at which to sell the position. Default is 2.0. | 2.0 |
| STOP\_LOSS\_PERCENT | The percentage loss at which to sell the position. Default is 1.0. | 1.0 |

## **Trading Strategy**

The bot's trading logic is based on a combination of technical indicators and risk management rules:

### **Buy Signal**

A buy signal is generated when both of these conditions are met:

1. **RSI (Relative Strength Index)**: The RSI is below the RSI\_BUY\_THRESHOLD (default 50), indicating the asset is not overbought.  
2. **MACD (Moving Average Convergence Divergence)**: The MACD line crosses above the Signal line, which is a bullish crossover signal.

### **Sell Signal**

The bot will sell a position if any of the following conditions are met:

1. **Take-Profit**: The current price rises by TAKE\_PROFIT\_PERCENT (default 2.0%) from the buy price.  
2. **Stop-Loss**: The current price falls by STOP\_LOSS\_PERCENT (default 1.0%) from the buy price.  
3. **Technical Signal**: The RSI is above the RSI\_OVERBOUGHT value (default 70\) AND the MACD line crosses below the Signal line.

## **Disclaimer**

**Trading cryptocurrencies involves substantial risk and is not suitable for everyone.** The bot's strategy is for demonstration and educational purposes. Past performance is not indicative of future results. You are solely responsible for all trading and investment decisions.