# 🤖 Binance Futures Trading Bot — v7 (Local Stable, Dry-Run)
Automated Binance Futures trading bot with Telegram signal parsing, strict **1% TP/SL from the filled average price**, and continuous verification & cleanup.  
Runs on **Testnet (default)** or **Live**, with a threaded watchdog and verbose user alerts.

---

## ⚙️ Overview
This bot listens to a specified Telegram channel for LONG/SHORT trade calls — even with minor typos or formatting differences —  
then executes Binance Futures entries automatically with built-in risk control.

### ✅ Supported Examples
```
Long Setup #PEOPLEUSDT
Coin: #BNBUSDT | Entry 1140–1165 | SL 1110 | TP 1180
Short Setup: BTCUSDT | Entry: CMP | SL 104000 | TP 99000
```

---

## 🧠 Key Features
- Flexible & typo-tolerant signal parser  
- 1% TP/SL from **actual filled entry** (not pre-signal price)  
- Live verification + watchdog re-attach if TP/SL vanish  
- Auto-cleanup when position closes or times out  
- Verbose Telegram alerts (`VERBOSE_ALERTS=true`)  
- Safe **Dry-Run mode** and **Testnet/Live toggle**  
- Detailed event logging via `bot.log`

---

## 🧩 Tech Stack
**Language:** Python 3  
**Libraries:**  
`python-binance` · `telethon` · `python-dotenv` · `logging` · `threading` · `asyncio`

---

## 🛠 Setup
```bash
# 1. Clone repo
git clone https://github.com/shivpatel-dev/binance-futures-bot.git
cd binance-futures-bot

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Fill in your Telegram + Binance keys

# 4. Run the bot
python binance_futures_bot.py
```

---

## ⚙️ .env Example
```env
# TELEGRAM
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcd1234abcd1234abcd1234abcd1234
TELEGRAM_PHONE=+911234567890
TELEGRAM_SESSION=trading_bot
TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx

# BINANCE
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
BINANCE_TESTNET=true

# CONFIG
TRADE_AMOUNT=100
DEFAULT_LEVERAGE=5
TPSL_PCT=0.01
DRY_RUN=true
LOG_LEVEL=INFO
VERBOSE_ALERTS=true
```

---

## 📜 Sample Log Output
```
2025-11-05 09:42:47 | INFO | Booting… ENV=TESTNET WEBSOCKET_ENABLED=True, PARTIAL_POLICY=ATTACH_AND_CANCEL
2025-11-05 09:42:50 | INFO | ALERT: Position mode: ONE_WAY
2025-11-05 09:42:50 | INFO | ALERT: Probe PEOPLEUSDT: present=True, mark=0.01008
2025-11-05 09:43:10 | INFO | ALERT: [BNBUSDT] Waiting up to 120s for range 1140.0–1165.0
2025-11-05 09:43:54 | INFO | Bot disconnected cleanly.
```

---

## 🧩 Architecture Overview
```
Telegram (signals)
        ↓
Signal Parser → Validation → Order Flow → TP/SL Attach → Watchdog → Auto-Cleanup
        ↓
    Binance Futures (Testnet/Live)
```

---

## ⚖️ License
This project is released under the [MIT License](./LICENSE).

---

## 💬 Author
**Shiv Patel** — Automation & Operations Specialist  
📫 [LinkedIn](https://linkedin.com/in/shiv-patel-71421b189) • [GitHub](https://github.com/shivpatel-dev)
