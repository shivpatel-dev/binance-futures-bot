# 🤖 Binance Futures Trading Bot — v6
Automated Binance Futures trading bot with Telegram signal parsing, strict 1% TP/SL from the *filled average price*, and watchdog verification/cleanup. Works on **Testnet** (default) or **Live**.

## ⚙️ Overview
- Listens to a specified Telegram channel for LONG/SHORT signals (accepts flexible formats like “Long Setup”, “Coin: #PEOPLEUSDT”, ranges, CMP).
- Executes market entries on Binance Futures.
- Attaches TP/SL immediately, verifies they stay live, and cleans up after closure.

## 🧠 Key Features
- Flexible signal parser (variants & typos tolerated)
- 1% TP/SL from **filled** price
- Live verification + watchdog re-attach
- Auto-cancel leftovers when flat
- Verbose Telegram DM alerts (optional)
- Testnet/Live switch via `.env`

## 🧩 Tech Stack
Python 3 · `python-binance` · `telethon` · `python-dotenv` · `logging` · `threading` · `asyncio`

## 🛠 Setup
```bash
git clone https://github.com/shivpatel-dev/binance-futures-bot.git
cd binance-futures-bot
pip install -r requirements.txt
