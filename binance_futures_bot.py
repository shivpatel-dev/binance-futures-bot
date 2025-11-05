#!/usr/bin/env python3
# Binance Futures Bot — v7 Local Stable (Dry-Run)
# (STRICT 1% TP/SL + Softer Signal Parser + Verbose User Alerts)
#
# Key changes from v5:
# - Softer parser: accepts variants like "Long Setup", "Long Set-Up", "Long Setp",
#   symbol forms like "#PEOPLEUSDT", "PEOPLE/USDT", "PEOPLE-USDT", "peopleusd", etc.
#   Also recognizes a leading line like "Coin: #PEOPLEUSDT".
# - Verbose user-level alerts: toggle via VERBOSE_ALERTS=true to get a DM for
#   every seen post, parser rejects, waits, and final reasons (timeout, filters, etc.).
# - LOG_LEVEL env to adjust logging verbosity in bot.log.
# - Environment probe at boot (TESTNET/LIVE) and optional symbol probes.
# - All prior protections preserved: TP/SL always ±TPSL_PCT from *filled avg price*,
#   verification & watchdog to keep TP/SL present, cleanup when flat.
#
# .env additions you can use (examples):
#   ALERT_ENABLED=true
#   ALERT_TARGET=me              # or @your_username or numeric id
#   DUMP_IGNORED=true
#   DUMP_IGNORED_TO_ME=true
#   VERBOSE_ALERTS=true
#   LOG_LEVEL=DEBUG              # or INFO/WARNING
#   BINANCE_TESTNET=true         # default true; set false for live
#   PROBE_SYMBOLS=PEOPLEUSDT,BTCUSDT
#
# NOTE: For live trading set BINANCE_TESTNET=false.

import os, re, time, logging, threading, asyncio, textwrap
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Optional, Any, Dict, Tuple, Callable, Set, List

from dotenv import load_dotenv
from telethon import TelegramClient, events

from binance.client import Client as BinanceClient
from binance.enums import *
from binance.exceptions import BinanceAPIException, BinanceOrderException
from binance.streams import ThreadedWebsocketManager

# ---------------------- ENV ----------------------
load_dotenv(Path(__file__).with_name(".env"))

def _env_bool(key: str, default: bool=False) -> bool:
    v = os.getenv(key, str(default)).strip().lower()
    return v in ("1","true","yes","on")

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)).strip())
    except Exception:
        return default

TG_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TG_PHONE = os.getenv("TELEGRAM_PHONE", "").strip()
TG_SESSION = os.getenv("TELEGRAM_SESSION", "trading_bot").strip()
CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "0"))

BINANCE_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SEC = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_TESTNET = _env_bool("BINANCE_TESTNET", True)

# Sizing: TRADE_AMOUNT is *margin*; bot uses notional = TRADE_AMOUNT * DEFAULT_LEVERAGE
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "100"))          # margin capital
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))
ORDER_LIFETIME_SEC = int(os.getenv("ORDER_LIFETIME_SEC", "120"))
WORKING_TYPE = os.getenv("WORKING_TYPE", "MARK_PRICE").strip().upper()
PARTIAL_POLICY = os.getenv("PARTIAL_POLICY", "ATTACH_AND_CANCEL").strip().upper()
WEBSOCKET_ENABLED = _env_bool("WEBSOCKET_ENABLED", True)
DRY_RUN = _env_bool("DRY_RUN", False)
ALERT_ENABLED = _env_bool("ALERT_ENABLED", False)
ALERT_TARGET_RAW = os.getenv("ALERT_TARGET", "me").strip()
DUMP_IGNORED = _env_bool("DUMP_IGNORED", False)
DUMP_IGNORED_TO_ME = _env_bool("DUMP_IGNORED_TO_ME", False)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
VERBOSE_ALERTS = _env_bool("VERBOSE_ALERTS", False)
PROBE_SYMBOLS_RAW = os.getenv("PROBE_SYMBOLS", "").strip()

# EXACT TP/SL distance from *filled* entry
TPSL_PCT = _env_float("TPSL_PCT", 0.01)  # default 1%

# ---------------------- LOGGING ----------------------
LOG_FILE = Path(__file__).with_name("bot.log")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("vipbot")

# ---------------------- TELEGRAM ----------------------
client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)

# --- DEBUG PATCH START ---
# Logs every Telegram message received by this account (any chat)
@client.on(events.NewMessage)
async def debug_all(event):
    try:
        text = (event.raw_text or "").strip()
        sender = event.chat_id
        log.info(f"[DEBUG MSG] From chat {sender} — {text[:150]}")
    except Exception as e:
        log.warning(f"[DEBUG ERROR] {e}")
# --- DEBUG PATCH END ---

_alert_peer_cached: Optional[Any] = None

async def _resolve_alert_peer():
    global _alert_peer_cached
    if _alert_peer_cached is not None:
        return _alert_peer_cached
    target = ALERT_TARGET_RAW
    try:
        if target.lower() == "me":
            me = await client.get_me()
            _alert_peer_cached = me
        elif target.startswith("@"):  # username
            _alert_peer_cached = await client.get_entity(target)
        else:
            _alert_peer_cached = int(target)
    except Exception:
        _alert_peer_cached = "me"
    return _alert_peer_cached

def alert(text: str):
    """Thread-safe alert.
    - If main asyncio loop is running on main thread, send DM.
    - Otherwise, log to file/console silently (no warnings, no errors).
    """
    if not ALERT_ENABLED:
        return
    msg = textwrap.shorten(text, width=3500, placeholder=' ...')
    try:
        loop = getattr(client, 'loop', None)
        import threading as _th
        if loop and loop.is_running() and _th.current_thread() is _th.main_thread():
            try:
                asyncio.run_coroutine_threadsafe(_alert_send(msg), loop)
                return
            except Exception:
                pass
        # Fallback: just log (no error details)
        log.info(f'ALERT: {msg}')
    except Exception:
        # Ultra-safe: never raise from alert path
        log.info(f'ALERT: {msg}')

def valert(msg: str):
    if VERBOSE_ALERTS:
        alert(msg)

# ---------------------- BINANCE FUTURES (set testnet/live via env) ----------------------
binance = BinanceClient(BINANCE_KEY, BINANCE_SEC, testnet=BINANCE_TESTNET)

twm = None
ORDERS: Dict[int, Dict[str, Any]] = {}
ORDERS_LOCK = threading.Lock()
TOUCHED_SYMBOLS: Set[str] = set()
TOUCHED_LOCK = threading.Lock()

_FILTERS: Dict[str, Dict[str, Decimal]] = {}
_position_dual = None

TRANSIENT_CODES = {-1001, -1007, -1021, -1022}

def with_retries(fn: Callable, *, attempts=3, backoff=0.6):
    def _wrap(*args, **kwargs):
        delay = backoff
        last_exc = None
        for _ in range(attempts):
            try:
                return fn(*args, **kwargs)
            except BinanceAPIException as e:
                last_exc = e
                if e.code in TRANSIENT_CODES:
                    time.sleep(delay); delay *= 2; continue
                raise
            except Exception as e:
                last_exc = e
                time.sleep(delay); delay *= 2; continue
        if last_exc: raise last_exc
    return _wrap

# ---------------------- Exchange helpers ----------------------
@with_retries
def _exchange_info():
    if not hasattr(_exchange_info, "_cache"):
        _exchange_info._cache = binance.futures_exchange_info()
    return _exchange_info._cache


def _load_filters(symbol: str):
    if symbol in _FILTERS:
        return _FILTERS[symbol]
    info = _exchange_info()
    step = Decimal("0.0001"); qstep = Decimal("0.001")
    min_qty = Decimal("0"); min_notional = Decimal("0")
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    step = Decimal(f["tickSize"])
                elif f.get("filterType") == "LOT_SIZE":
                    qstep = Decimal(f["stepSize"]); min_qty = Decimal(f.get("minQty","0"))
                elif f.get("filterType") in ("MIN_NOTIONAL","NOTIONAL"):
                    min_notional = Decimal(str(f.get("notional", f.get("minNotional", "0"))))
            break
    _FILTERS[symbol] = {"tick": step, "qstep": qstep, "min_qty": min_qty, "min_notional": min_notional}
    return _FILTERS[symbol]


def price_step(symbol: str) -> Decimal: return _load_filters(symbol)["tick"]
def qty_step(symbol: str) -> Decimal: return _load_filters(symbol)["qstep"]
def min_qty(symbol: str) -> Decimal: return _load_filters(symbol)["min_qty"]
def min_notional(symbol: str) -> Decimal: return _load_filters(symbol)["min_notional"]


def round_to_step(value: Decimal, step: Decimal) -> Decimal:
    q = (value / step).to_integral_value(rounding=ROUND_DOWN); return (q * step).normalize()


def _scale_of(step: Decimal) -> int:
    return max(0, -step.normalize().as_tuple().exponent)


def fmt_price(symbol: str, px: float) -> str:
    step = price_step(symbol)
    d = Decimal(str(px))
    q = (d / step).to_integral_value(rounding=ROUND_DOWN) * step
    return f"{q:.{_scale_of(step)}f}"


def fmt_qty(symbol: str, qty: Decimal) -> str:
    step = qty_step(symbol)
    d = Decimal(qty)
    q = (d / step).to_integral_value(rounding=ROUND_DOWN) * step
    return f"{q:.{_scale_of(step)}f}"


def notional_to_qty(symbol: str, notional_usdt: float, price: float) -> Decimal:
    step = qty_step(symbol); raw = Decimal(str(notional_usdt)) / Decimal(str(price)); return round_to_step(raw, step)


def ensure_min_qty_and_notional(symbol: str, price: float, qty: Decimal) -> Decimal:
    f = _load_filters(symbol)
    notional = Decimal(str(price)) * qty
    if f["min_notional"] > 0 and notional < f["min_notional"]:
        needed = (f["min_notional"] / Decimal(str(price))); qty = round_to_step(needed, f["qstep"])
    if f["min_qty"] > 0 and qty < f["min_qty"]:
        qty = round_to_step(f["min_qty"], f["qstep"])
    return qty


def ensure_leverage(symbol: str, lev: int):
    if DRY_RUN: return
    try:
        with_retries(binance.futures_change_leverage)(symbol=symbol, leverage=lev)
    except BinanceAPIException as e:
        log.warning(f"Leverage warn {symbol}: {e.message}")


def mark_price(symbol: str) -> float:
    try:
        mp = with_retries(binance.futures_mark_price)(symbol=symbol)
        return float(mp["markPrice"])
    except Exception:
        return 0.0


def get_position_mode() -> bool:
    global _position_dual
    if _position_dual is not None: return _position_dual
    try:
        pm = with_retries(binance.futures_get_position_mode)()
        _position_dual = pm.get("dualSidePosition", False)
    except Exception:
        _position_dual = False
    return _position_dual


def position_side_for_entry(side: str) -> Optional[str]:
    if not get_position_mode(): return None
    return "LONG" if side.upper() == "LONG" else "SHORT"

# ---------------------- Softer STRICT Parser ----------------------
# We still require explicit side and an entry, but we accept more symbol/heading spellings.

# Numbers
NUM = r"\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\$?"
RANGE = rf"(?:{NUM}\s*[-–—]\s*{NUM})|(?:{NUM})|(?:CMP\b)"

# Symbol pattern: base + (USDT or USD), with flexible separators and optional leading 'Coin:'
# Examples: "#PEOPLEUSDT", "PEOPLE/USDT", "people-usdt", "people usd"
SYM = r"\#?(?P<base>[A-Z0-9]{2,})\s*(?:[\/\-\s]?\s*(?:USDT|USD))\b"

# Entry synonyms are already broad; keep them
ENTRY_WORDS = r"(?:entry(?:\s*zone)?|entries|entry\s*range|buy\s*between|buy\s*range|cmp)"
TP_WORDS    = r"(?:tp|targets?|take\s*profit(?:s)?|tps?)"
SL_WORDS    = r"(?:sl|stop\s*loss|stoploss|stop\s*loss\s*at|stop\s*at|stop)"

# Side tokens: allow "Long Setup", "Long Set-Up", "Long Setp", and plain LONG/SHORT
SIDE_WORDS  = r"(?:\b(long|short)\b|(?:enter\s+(?:long|short))|(?:long|short)\s*set[\-\s]?up|(?:long|short)\s*setup|(?:long|short)\s*set-?p)"

SIG_RE_A = re.compile(
    rf"""(?P<side>{SIDE_WORDS}).*?(?P<symbol>{SYM}).*?(?:{ENTRY_WORDS})\s*[:\-]?\s*(?P<entry>{RANGE})
        (?:.*?(?:{TP_WORDS})\s*[:\-]?\s*(?P<tp_list>{NUM}(?:\s*[,\-\s]\s*{NUM})*))?
        (?:.*?(?:{SL_WORDS})\s*[:\-]?\s*(?P<sl>{NUM}))?
    """, re.I|re.S|re.X)

SIG_RE_B = re.compile(
    rf"""(?P<symbol>{SYM}).*?(?P<side>{SIDE_WORDS}).*?(?:{ENTRY_WORDS})\s*[:\-]?\s*(?P<entry>{RANGE})
        (?:.*?(?:{TP_WORDS})\s*[:\-]?\s*(?P<tp_list>{NUM}(?:\s*[,\-\s]\s*{NUM})*))?
        (?:.*?(?:{SL_WORDS})\s*[:\-]?\s*(?P<sl>{NUM}))?
    """, re.I|re.S|re.X)

# Orderless fallback chunks (for robustness)
ANY_ORDER = re.compile(
    rf"""(?P<symbol>{SYM})|(?P<side>{SIDE_WORDS})|
        (?P<entry_label>{ENTRY_WORDS})\s*[:\-]?\s*(?P<entry>{RANGE})|
        (?P<tp_label>{TP_WORDS})\s*[:\-]?\s*(?P<tp_list>{NUM}(?:\s*[,\-\s]\s*{NUM})*)|
        (?P<sl_label>{SL_WORDS})\s*[:\-]?\s*(?P<sl>{NUM})
    """, re.I|re.S|re.X)

# Leading "Coin:" fallback (optional)
COIN_LINE_RE = re.compile(r"coin\s*[:\-]?\s*#?([A-Z0-9]{2,})\s*(?:[\/\-\s]?(?:USDT|USD))\b", re.I)


def _num_clean(s: str) -> str: return s.replace(",", "").replace("$", "").strip()


def _first_num(seq: str):
    if not seq: return None
    parts = re.split(r"[\s,\-]+", seq.strip())
    for p in parts:
        p = p.strip()
        if p:
            try: return float(_num_clean(p))
            except: pass
    return None


def _parse_entry(entry_raw: str):
    if not entry_raw: return None, None, None
    E = entry_raw.strip().upper().replace("–","-").replace("—","-")
    if E == "CMP": return None, ("CMP","CMP"), True
    E = E.replace(" ", "")
    if "-" in E:
        a_str, b_str = E.split("-", 1)
        a, b = float(_num_clean(a_str)), float(_num_clean(b_str))
        lo, hi = (a, b) if a <= b else (b, a)
        return None, (lo, hi), False
    val = float(_num_clean(E)); return val, (val, val), False


def _normalize_side(raw_side: str) -> Optional[str]:
    if not raw_side: return None
    s = raw_side.lower()
    if "short" in s: return "SHORT"
    if "long" in s:  return "LONG"
    return None


def _normalize_symbol_base(base: str) -> Optional[str]:
    if not base: return None
    return base.upper()


def parse_signal(text: str):
    txt = text or ""
    m = SIG_RE_A.search(txt) or SIG_RE_B.search(txt)
    base = None; side = None
    if m:
        side = _normalize_side(m.group("side"))
        base = _normalize_symbol_base(m.group("base")) if m.group("base") else None
        if not (side and base):
            return None
        symbol = f"{base}USDT"  # normalize even if USD matched
        entry = m.group("entry"); tp_list = m.group("tp_list"); sl = m.group("sl")
        entry_val, rng, is_cmp = _parse_entry(entry)
        return {
            "side": side,
            "symbol": symbol,
            "entry": entry_val,
            "range_bounds": rng if rng else ("CMP","CMP") if is_cmp else None,
            "tp": _first_num(tp_list),
            "sl": float(_num_clean(sl)) if sl else None,
            "is_cmp": is_cmp,
        }

    # fallback: orderless match
    side=base=entry_field=tp_list=sl=None
    for mm in ANY_ORDER.finditer(txt):
        if mm.group("base") and not base: base = _normalize_symbol_base(mm.group("base"))
        if mm.group("side") and not side: side = _normalize_side(mm.group("side"))
        if mm.group("entry") and not entry_field: entry_field = mm.group("entry")
        if mm.group("tp_list") and not tp_list: tp_list = mm.group("tp_list")
        if mm.group("sl") and not sl: sl = mm.group("sl")

    # bonus fallback: leading "Coin: ..." line
    if not base:
        cm = COIN_LINE_RE.search(txt)
        if cm:
            base = _normalize_symbol_base(cm.group(1))

    if not base or not side or not entry_field:
        return None

    symbol = f"{base}USDT"
    entry_val, rng, is_cmp = _parse_entry(entry_field)
    return {
        "side": side,
        "symbol": symbol,
        "entry": entry_val,
        "range_bounds": rng if rng else ("CMP","CMP") if is_cmp else None,
        "tp": _first_num(tp_list),
        "sl": float(_num_clean(sl)) if sl else None,
        "is_cmp": is_cmp,
    }

# ---------------------- Attempt watchdog ----------------------
class Attempt:
    def __init__(self, symbol: str, entry_type: str, t0: float):
        self.symbol = symbol; self.entry_type = entry_type; self.t0 = t0; self.done=False
        self._timer = threading.Timer(ORDER_LIFETIME_SEC + 6, self._timeout_fire)
        self._timer.daemon = True; self._timer.start()
    def _timeout_fire(self):
        if not self.done:
            elapsed = int(time.time()-self.t0)
            alert(f"⏭️ [{self.symbol}] {self.entry_type} — no outcome by t+{elapsed}s (timeout watchdog).")
            log.warning(f"[{self.symbol}] Watchdog timeout fired."); self.done=True
    def finish(self, msg: str):
        if not self.done:
            self.done = True
            try:
                self._timer.cancel()
            except:
                pass
            # ✅ Always write to bot.log
            log.info(msg)
            # ✅ Still try to send to Telegram
            try:
                alert(msg)
            except Exception as e:
                log.error(f"Failed to send alert: {e}")

# ---------------------- TP/SL ATTACH ----------------------

def attach_tp_sl(symbol: str, side: str, qty: Decimal, tp_px: float, sl_px: float, close_side, place_sl: bool):
    if qty <= 0: return
    pos_side = position_side_for_entry(side)
    tp_s = fmt_price(symbol, tp_px)
    sl_s = fmt_price(symbol, sl_px)
    qty_s = fmt_qty(symbol, qty)

    if DRY_RUN:
        log.info(f"[{symbol}] (DRY_RUN) Attach TP {tp_s} qty={qty_s} | SL {sl_s}")
        alert(f"[{symbol}] (DRY_RUN) TP {tp_s} qty={qty_s}; SL {sl_s}")
        return
    try:
        # TP reduce-only LIMIT
        args = dict(symbol=symbol, side=close_side, type=FUTURE_ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC, price=tp_s, quantity=qty_s, reduceOnly=True)
        if pos_side: args["positionSide"] = pos_side
        with_retries(binance.futures_create_order)(**args)
        # SL — first try closePosition=True
        if place_sl:
            args2 = dict(symbol=symbol, side=close_side, type=FUTURE_ORDER_TYPE_STOP_MARKET,
                         stopPrice=sl_s, closePosition=True, workingType=WORKING_TYPE)
            if pos_side: args2["positionSide"] = pos_side
            try:
                with_retries(binance.futures_create_order)(**args2)
            except (BinanceAPIException, BinanceOrderException) as e:
                # fallback: qty-based reduceOnly STOP-MARKET
                log.warning(f"[{symbol}] SL closePosition rejected, fallback to qty-based: {e}")
                args3 = dict(symbol=symbol, side=close_side, type=FUTURE_ORDER_TYPE_STOP_MARKET,
                             stopPrice=sl_s, quantity=qty_s, reduceOnly=True, workingType=WORKING_TYPE)
                if pos_side: args3["positionSide"] = pos_side
                with_retries(binance.futures_create_order)(**args3)
        alert(f"[{symbol}] TP {tp_s} attached qty={qty_s}. SL set.")
    except (BinanceAPIException, BinanceOrderException) as e:
        msg = f"[{symbol}] TP/SL placement failed: {e}"
        log.error(msg); alert("❌ " + msg)

# ---------------------- Cleanup helpers ----------------------

def _position_amt_both(symbol: str) -> Tuple[Decimal, Decimal]:
    try:
        info = with_retries(binance.futures_position_information)(symbol=symbol)
        dual = get_position_mode()
        if dual:
            l = s = Decimal("0")
            for p in info:
                if p.get("symbol")==symbol and p.get("positionSide")=="LONG": l = Decimal(p["positionAmt"])
                if p.get("symbol")==symbol and p.get("positionSide")=="SHORT": s = Decimal(p["positionAmt"])
            return l, s
        else:
            for p in info:
                if p.get("symbol")==symbol:
                    amt = Decimal(p["positionAmt"]); return (amt if amt>0 else Decimal("0"), -amt if amt<0 else Decimal("0"))
    except Exception:
        return Decimal("0"), Decimal("0")
    return Decimal("0"), Decimal("0")


def cancel_all_for(symbol: str):
    try:
        with_retries(binance.futures_cancel_all_open_orders)(symbol=symbol)
        alert(f" [{symbol}] Auto-cleanup: canceled remaining open orders.")
    except Exception as e:
        log.warning(f"[{symbol}] Auto-cleanup cancel failed: {e}")


def cleanup_if_flat(symbol: str):
    l, s = _position_amt_both(symbol)
    if l == 0 and s == 0:
        cancel_all_for(symbol)


def monitor_and_autocleanup(symbol: str, stop_after_s: int = 3600):
    start = time.time(); last=None
    while time.time()-start < stop_after_s:
        l, s = _position_amt_both(symbol)
        state = "OPEN" if (l != 0 or s != 0) else "FLAT"
        if state != last:
            log.info(f"[{symbol}] Position watch: {state} (L={l}, S={s}).")
            last = state
        if state == "FLAT":
            cancel_all_for(symbol); return
        time.sleep(5)

# Periodic sweeper (kept)

def _sweeper():
    while True:
        time.sleep(20)
        with TOUCHED_LOCK:
            symbols = list(TOUCHED_SYMBOLS)
        for sym in symbols:
            try:
                cleanup_if_flat(sym)
            except Exception as e:
                log.warning(f"[{sym}] sweeper error: {e}")

threading.Thread(target=_sweeper, daemon=True).start()

# ---------------------- TP/SL verification helpers ----------------------

def _open_orders_for(symbol: str):
    try:
        return with_retries(binance.futures_get_open_orders)(symbol=symbol)
    except Exception:
        return []


def _position_qty(symbol: str, side: str) -> Decimal:
    long_q, short_q = _position_amt_both(symbol)
    return long_q if side == "LONG" else short_q


def _has_tp_sl(symbol: str, side: str, tp_px: float, sl_px: float) -> Tuple[bool, bool]:
    oo = _open_orders_for(symbol)
    close_side = SIDE_SELL if side == "LONG" else SIDE_BUY
    tp_s = fmt_price(symbol, tp_px)
    has_tp = any(o.get("side") == close_side and o.get("type") == FUTURE_ORDER_TYPE_LIMIT and
                 fmt_price(symbol, float(o.get("price","0"))) == tp_s for o in oo)
    has_sl = any(o.get("side") == close_side and o.get("type") in (FUTURE_ORDER_TYPE_STOP, FUTURE_ORDER_TYPE_STOP_MARKET)
                 for o in oo)
    return has_tp, has_sl


def ensure_tp_sl_live(symbol: str, side: str, tp_px: float, sl_px: float, tries: int = 4, sleep_s: float = 1.0):
    close_side = SIDE_SELL if side == "LONG" else SIDE_BUY
    pos_side = position_side_for_entry(side)
    for attempt in range(tries):
        try:
            pos_qty = _position_qty(symbol, side)
            has_tp, has_sl = _has_tp_sl(symbol, side, tp_px, sl_px)
            if pos_qty <= 0:
                cancel_all_for(symbol)
                return True
            if not has_tp:
                attach_tp_sl(symbol, side, pos_qty, tp_px, sl_px, close_side, place_sl=False)
            if not has_sl:
                attach_tp_sl(symbol, side, pos_qty, tp_px, sl_px, close_side, place_sl=True)
            has_tp, has_sl = _has_tp_sl(symbol, side, tp_px, sl_px)
            if has_tp and has_sl:
                return True
        except Exception as e:
            log.warning(f"[{symbol}] ensure_tp_sl_live try#{attempt+1} error: {e}")
        time.sleep(sleep_s * (attempt+1))
    return False


def watchdog_protection(symbol: str, side: str, tp_px: float, sl_px: float, seconds: int = 600):
    t0 = time.time()
    while time.time() - t0 < seconds:
        l, s = _position_amt_both(symbol)
        if l == 0 and s == 0:
            cancel_all_for(symbol)
            return
        ensure_tp_sl_live(symbol, side, tp_px, sl_px, tries=2, sleep_s=1.0)
        time.sleep(5)

# ---------------------- Order flow ----------------------

def place_limit_then_manage(sig: dict):
    symbol = sig["symbol"]; side = sig["side"]
    with TOUCHED_LOCK: TOUCHED_SYMBOLS.add(symbol)
    t0 = time.time()
    outcome = Attempt(symbol, "PENDING", t0)

    try:
        mp = mark_price(symbol)
        rng = sig.get("range_bounds")
        is_cmp = sig.get("is_cmp", False)

        def wait_for_price_in(b_lo: float, b_hi: float, timeout_sec: int) -> float:
            start_t = time.time(); zero_streak = 0; last_beat = 0
            while time.time()-start_t < timeout_sec:
                m = mark_price(symbol)
                if m and (b_lo <= m <= b_hi): return m
                if not m:
                    zero_streak += 1
                    if zero_streak >= 5:
                        return 0.0
                else:
                    zero_streak = 0
                if time.time()-last_beat > 30:
                    alert(f"⏳ [{symbol}] waiting… need mark in {b_lo}–{b_hi}, current={m if m else 'n/a'}")
                    last_beat = time.time()
                time.sleep(1)
            return 0.0

        # CMP immediate
        if is_cmp:
            if not mp or mp <= 0:
                outcome.entry_type = "CMP"
                alert(f"⏭️ [{symbol}] CMP — no mark price available (price feed empty).")
                return
            px = float(fmt_price(symbol, mp))
            entry_type = "CMP"
            valert(f"⚙️ [{symbol}] CMP mode — will market-enter @ current mark {mp}")

        # Range-only
        elif rng and isinstance(rng, tuple) and rng != ("CMP","CMP"):
            lo, hi = rng; entry_type = "RangeOnly"
            if mp and (lo <= mp <= hi):
                px = float(fmt_price(symbol, mp))
                alert(f"[{symbol}] Range match NOW → MARKET @ {px} (range {lo}–{hi})")
            else:
                valert(f"⏳ [{symbol}] Waiting up to {ORDER_LIFETIME_SEC}s for range {lo}–{hi} (current mark={mp if mp else 'n/a'})")
                m2 = wait_for_price_in(lo, hi, ORDER_LIFETIME_SEC)
                if m2 and m2 > 0: px = float(fmt_price(symbol, m2))
                else:
                    elapsed = int(time.time()-t0); outcome.entry_type = entry_type
                    outcome.finish(f"⏭️ [{symbol}] SKIPPED — RangeOnly {lo}–{hi}, mark={mp if mp else 'n/a'}, timed out at t+{elapsed}s."); return

        # Single price ±0.3% band
        else:
            entry_val = sig.get("entry")
            if entry_val is None and rng and isinstance(rng, tuple): entry_val = rng[0]
            if entry_val is None:
                outcome.entry_type = "Single+Range"
                outcome.finish(f"⏭️ [{symbol}] SKIPPED — Could not resolve entry price."); return
            band = 0.003; band_lo = entry_val*(1.0-band); band_hi = entry_val*(1.0+band)
            entry_type = "Single+Range"
            if mp and (band_lo <= mp <= band_hi):
                px = float(fmt_price(symbol, mp))
                alert(f"[{symbol}] Single band NOW → MARKET @ {px} (±0.3% of {entry_val})")
            else:
                valert(f"⏳ [{symbol}] Waiting up to {ORDER_LIFETIME_SEC}s for ±0.3% around {entry_val} ({band_lo}–{band_hi}), current mark={mp if mp else 'n/a'}")
                m2 = wait_for_price_in(band_lo, band_hi, ORDER_LIFETIME_SEC)
                if m2 and m2 > 0: px = float(fmt_price(symbol, m2))
                else:
                    elapsed = int(time.time()-t0); outcome.entry_type = entry_type
                    outcome.finish(f"⏭️ [{symbol}] SKIPPED — Single+Range ±0.3% of {entry_val}, mark={mp if mp else 'n/a'}, timed out at t+{elapsed}s."); return

        # --- Leverage-aware sizing ---
        notional_usdt = TRADE_AMOUNT * DEFAULT_LEVERAGE
        qty_total = ensure_min_qty_and_notional(symbol, float(px), notional_to_qty(symbol, notional_usdt, float(px)))
        if qty_total <= 0:
            outcome.entry_type = entry_type
            outcome.finish(f"⏭️ [{symbol}] SKIPPED — Qty invalid for ${notional_usdt} at {px} (min filters)."); return

        ensure_leverage(symbol, DEFAULT_LEVERAGE)
        order_side = SIDE_BUY if side == "LONG" else SIDE_SELL
        close_side = SIDE_SELL if side == "LONG" else SIDE_BUY
        pos_side = position_side_for_entry(side)

        qty_s = fmt_qty(symbol, qty_total)
        px_s  = fmt_price(symbol, float(px))

        pre = (f"[{symbol}] {side} | {entry_type} | MARKET qty≈{qty_s} @ ~{px_s} | "
               f"notional≈${notional_usdt} (lev {DEFAULT_LEVERAGE}x, margin ${TRADE_AMOUNT})")
        alert(" New entry attempt\n" + pre)

        if DRY_RUN:
            outcome.entry_type = entry_type
            entry_px = float(px)
            tp_px = entry_px * (1.0 + (TPSL_PCT if side == "LONG" else -TPSL_PCT))
            sl_px = entry_px * (1.0 - (TPSL_PCT if side == "LONG" else -TPSL_PCT))
            outcome.finish(f"✅ [{symbol}] (DRY_RUN) Would enter @ ~{px_s}; TP {fmt_price(symbol, tp_px)}; SL {fmt_price(symbol, sl_px)}")
            return

        try:
            args = dict(symbol=symbol, side=order_side, type=FUTURE_ORDER_TYPE_MARKET, quantity=qty_s)
            if pos_side: args["positionSide"] = pos_side
            entry_order = with_retries(binance.futures_create_order)(**args)
        except (BinanceAPIException, BinanceOrderException) as e:
            outcome.entry_type = entry_type
            outcome.finish(f"❌ [{symbol}] Entry order failed: {e}")
            return

        order_id = entry_order["orderId"]
        with ORDERS_LOCK:
            ORDERS[order_id] = {"symbol": symbol, "side": side, "entry_px": float(px), "qty_total": qty_total,
                                "qty_attached": Decimal("0"), "tp_px": None, "sl_px": None,
                                "close_side": close_side, "sl_placed": False, "t0": t0, "entry_type": entry_type}

        if WEBSOCKET_ENABLED: start_user_stream_once()
        threading.Thread(target=monitor_and_autocleanup, args=(symbol,), daemon=True).start()

        # quick poll for confirmation
        start_time = time.time()
        while time.time() - start_time < ORDER_LIFETIME_SEC:
            time.sleep(1)
            try:
                st = with_retries(binance.futures_get_order)(symbol=symbol, orderId=order_id)
            except BinanceAPIException:
                continue
            status = st.get("status"); executed = Decimal(str(st.get("executedQty","0")))
            if executed > 0:
                # Use EXACT filled avg price for TP/SL
                avg_px_raw = st.get("avgPrice") or st.get("price") or px_s
                try:
                    entry_px = float(avg_px_raw)
                except Exception:
                    entry_px = float(px)
                if side == "LONG":
                    tp_px = entry_px * (1.0 + TPSL_PCT)
                    sl_px = entry_px * (1.0 - TPSL_PCT)
                else:
                    tp_px = entry_px * (1.0 - TPSL_PCT)
                    sl_px = entry_px * (1.0 + TPSL_PCT)

                delta = executed; place_sl=True
                attach_tp_sl(symbol, side, delta, float(tp_px), float(sl_px), close_side, place_sl)
                with ORDERS_LOCK:
                    ctx = ORDERS.get(order_id)
                    if ctx:
                        ctx["tp_px"], ctx["sl_px"] = float(tp_px), float(sl_px)

                # Verify and start watchdog
                try:
                    ok = ensure_tp_sl_live(symbol, side, float(tp_px), float(sl_px), tries=4, sleep_s=1.2)
                    if not ok:
                        log.warning(f"[{symbol}] TP/SL verification did not confirm both orders.")
                except Exception as e:
                    log.warning(f"[{symbol}] TP/SL verification error: {e}")
                threading.Thread(target=watchdog_protection, args=(symbol, side, float(tp_px), float(sl_px),), daemon=True).start()

                avg_px_str = f" @ {fmt_price(symbol, entry_px)}" if entry_px else ""
                elapsed = int(time.time()-t0)
                outcome.entry_type = entry_type
                outcome.finish(f"✅ [{symbol}] Entry FILLED{avg_px_str}. Qty {fmt_qty(symbol, executed)}. ({entry_type}, t+{elapsed}s)")
                break
            if status in ("CANCELED","REJECTED","EXPIRED"):
                elapsed = int(time.time()-t0)
                outcome.entry_type = entry_type
                outcome.finish(f"❌ [{symbol}] Entry {status}. ({entry_type}, t+{elapsed}s)")
                break

        with ORDERS_LOCK: ORDERS.pop(order_id, None)

    except Exception as e:
        outcome.entry_type = outcome.entry_type if outcome.entry_type != "PENDING" else "UNKNOWN"
        outcome.finish(f"❌ [{symbol}] FAILED — {e}")
        raise

# ---------------------- Websocket ----------------------

twm_started=False; twm_lock=threading.Lock()

def on_futures_user_update(msg):
    try:
        if msg.get("e") != "ORDER_TRADE_UPDATE": return
        o = msg.get("o", {}); order_id = o.get("i"); symbol = o.get("s")
        with ORDERS_LOCK: ctx = ORDERS.get(order_id)
        if ctx:
            executed = Decimal(str(o.get("z","0")))
            delta = executed - ctx["qty_attached"]
            if delta > 0:
                place_sl = not ctx["sl_placed"]
                # Prefer using already-computed tp/sl if present; otherwise fall back to 1% from last entry_px
                tp_px = ctx.get("tp_px"); sl_px = ctx.get("sl_px")
                if tp_px is None or sl_px is None:
                    entry_px = float(ctx.get("entry_px", 0.0))
                    if entry_px > 0:
                        if ctx["side"] == "LONG":
                            tp_px = entry_px * (1.0 + TPSL_PCT); sl_px = entry_px * (1.0 - TPSL_PCT)
                        else:
                            tp_px = entry_px * (1.0 - TPSL_PCT); sl_px = entry_px * (1.0 + TPSL_PCT)
                attach_tp_sl(ctx["symbol"], ctx["side"], delta, float(tp_px), float(sl_px), ctx["close_side"], place_sl)
                with ORDERS_LOCK:
                    ctx["qty_attached"] += delta
                    if place_sl: ctx["sl_placed"] = True
        # Aggressive cleanup on any event
        cleanup_if_flat(symbol)
    except Exception as e:
        log.exception(f"WS update error: {e}"); alert(f"❌ WS handler error: {e}")


def start_user_stream_once():
    global twm, twm_started
    if not WEBSOCKET_ENABLED: return
    with twm_lock:
        if twm_started: return
        alert("Starting Binance futures user-data websocket…")
        twm = ThreadedWebsocketManager(api_key=BINANCE_KEY, api_secret=BINANCE_SEC, testnet=BINANCE_TESTNET)
        twm.start(); twm.start_futures_user_socket(callback=on_futures_user_update); twm_started=True

# ---------------------- De-dupe ----------------------
_processed_keys: Dict[Tuple[int,int,int], float] = {}
_processed_lock = threading.Lock()

def _gc_processed(ttl=3600):
    now=time.time(); dead=[k for k,t in _processed_keys.items() if now-t>ttl]
    for k in dead: _processed_keys.pop(k, None)

def _seen(chat_id:int,msg_id:int,edit_ts:int)->bool:
    with _processed_lock:
        _gc_processed(); key=(chat_id,msg_id,edit_ts)
        if key in _processed_keys: return True
        _processed_keys[key]=time.time(); return False


def _symbols_set_current_env() -> Set[str]:
    # Works for both testnet and live since _exchange_info() is env-bound
    try: return {s["symbol"] for s in _exchange_info()["symbols"]}
    except Exception: return set()



async def handler(event: events.NewMessage.Event):
    try:
        if not event or (event.raw_text or "").strip() == "":
            reason = "IGNORED: empty or non-text message"; log.info(reason); valert("ℹ️ " + reason); return
        if _seen(event.chat_id, event.id, int(event.date.timestamp())): return
        text=(event.raw_text or "").strip()
        valert(f" New post seen in {event.chat_id} (msg {event.id})\nPreview: {text[:200]}{'…' if len(text)>200 else ''}")

        log.info(f"[handle_event]  Message detected from channel: {event.chat_id}, text: {text[:100]}")
        sig = parse_signal(text)
        if not sig:
            reason="IGNORED: no signal pattern (need LONG/SHORT + <COIN>USDT + entry)"
            log.info(reason); valert("⚪ " + reason)
            if DUMP_IGNORED:
                dump = Path(__file__).with_name("ignored_samples.log")
                try:
                    prev = dump.read_text(encoding="utf-8") if dump.exists() else ""
                    dump.write_text(prev + f"\n---\n{time.ctime()}\n{text}\n", encoding="utf-8")
                except Exception: pass
                if DUMP_IGNORED_TO_ME: alert(f" IGNORED SAMPLE (raw):\n{text[:3000]}")
            return
        if not sig["symbol"].endswith("USDT"):
            reason=f"IGNORED: symbol {sig['symbol']} not USDT-margined"; log.info(reason); alert("ℹ️ " + reason); return
        symbols=_symbols_set_current_env()
        if sig["symbol"] not in symbols:
            reason=f"⏭️ SKIPPED — {sig['symbol']} unavailable on this environment."; log.info(reason); alert(reason); return
        alert(f" CONSIDERED signal → {sig['side']} {sig['symbol']} @ {sig['entry']} (range {sig['range_bounds']})")
        threading.Thread(target=place_limit_then_manage, args=(sig,), daemon=True).start()
    except Exception as e:
        log.exception(f"Handler error: {e}"); alert(f"❌ Handler error: {e}")



async def handler_edit(event: events.MessageEdited.Event):
    try:
        if not event or (event.raw_text or "").strip()=="": return
        if event.date and (time.time() - event.date.timestamp() > 5*60): return
        if _seen(event.chat_id, event.id, int(event.date.timestamp())): return
        text=(event.raw_text or "").strip(); log.info(f"[handle_event]  Message detected from channel: {event.chat_id}, text: {text[:100]}")
        sig = parse_signal(text)
        if not sig:
            valert("✏️ Edit seen but not a signal (parser miss)"); return
        alert(f"✏️ CONSIDERED (edit) → {sig['side']} {sig['symbol']} @ {sig['entry']} (range {sig['range_bounds']})")
        threading.Thread(target=place_limit_then_manage, args=(sig,), daemon=True).start()
    except Exception as e:
        log.exception(f"Edit handler error: {e}"); alert(f"❌ Edit handler error: {e}")


def log_position_mode():
    try:
        pm = with_retries(binance.futures_get_position_mode)()
        dual = pm.get("dualSidePosition", False)
        alert(f"Position mode: {'HEDGE' if dual else 'ONE_WAY'}")
    except Exception as e:
        alert(f"⚠️ Could not fetch position mode: {e}")


def _probe_symbols(symbols: List[str]):
    if not symbols: return
    try:
        avail = _symbols_set_current_env()
    except Exception as e:
        valert(f" Probe symbols failed: {e}"); return
    for sym in symbols:
        try:
            present = sym in avail
            mp = mark_price(sym) if present else 0.0
            valert(f" Probe {sym}: present={present}, mark={mp}")
        except Exception as e:
            valert(f" Probe {sym}: error {e}")


import asyncio

async def main():
    env_mode = "TESTNET" if BINANCE_TESTNET else "LIVE"
    boot_msg = (
        f"Booting… ENV={env_mode} WEBSOCKET_ENABLED={WEBSOCKET_ENABLED}, PARTIAL_POLICY={PARTIAL_POLICY}, "
        f"DRY_RUN={DRY_RUN}, LOG_LEVEL={LOG_LEVEL}, VERBOSE_ALERTS={VERBOSE_ALERTS} | "
        f"Channel: {CHANNEL_ID} | Log file: {LOG_FILE} | Keys: api=*** secret=***"
    )

    log.info(boot_msg)

    # Start Telegram client
    await client.start(phone=TG_PHONE)

    # Resolve Telegram channel entity (awaited)
    entity = await client.get_entity(int(CHANNEL_ID))
    log.info(f"Resolved channel entity: {entity.id}")

    # ✅ Attach event handlers dynamically inside main()
    async def handle_event(event):
        text = event.message.message
        log.info(f"[handle_event]  Message detected from channel: {event.chat_id}, text: {text[:100]}")
        sig = parse_signal(text)
        if not sig:
            return
        threading.Thread(target=place_limit_then_manage, args=(sig,), daemon=True).start()

    client.add_event_handler(handle_event, events.NewMessage(chats=[entity, int(CHANNEL_ID)]))
    client.add_event_handler(handle_event, events.MessageEdited(chats=[entity, int(CHANNEL_ID)]))

    # Send startup alert
    alert(" Bot online. " + boot_msg)

    # Log mode and probes
    log_position_mode()
    log.info(f"Listening on channel id {CHANNEL_ID} …")

    probes = [s.strip().upper() for s in PROBE_SYMBOLS_RAW.split(",") if s.strip()]
    _probe_symbols(probes)

    log.info(" Bot is now fully operational and waiting for messages…")

    # Keep the bot alive indefinitely
    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        log.warning(" Bot manually stopped by user.")
    finally:
        await client.disconnect()
        log.info("Bot disconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())