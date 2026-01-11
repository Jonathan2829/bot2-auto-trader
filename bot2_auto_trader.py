# bot2_auto_trader.py
# BOT2 AUTO TRADER (agresivo e independiente de BOT1)
# - PTB v20.7 async
# - Compra por señales BOT1 (signals_active NEW)
# - Y SI NO HAY señales, genera ENTRADAS propias (más agresivo) desde coins.json
# - Mensajes solo a chat AUTO

import os
import time
import hmac
import json
import math
import hashlib
import sqlite3
import threading
import traceback
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, List

import requests
import numpy as np
import pandas as pd
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# ENV / CONFIG
# =========================
SQLITE_FILE = os.getenv("SQLITE_FILE", "bot_state.sqlite3")
COINS_FILE = os.getenv("COINS_FILE", "coins.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_AUTO", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID_AUTO", "").strip()
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "").strip()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BOT_PIN = os.getenv("BOT_PIN", "").strip()

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.com").strip().rstrip("/")

# --- AUTO (más agresivo que BOT1) ---
AUTO_INTERVAL = os.getenv("AUTO_INTERVAL", "15m").strip()
AUTO_LIMIT = int(os.getenv("AUTO_LIMIT", "120"))

AUTO_SCORE_MIN = int(os.getenv("AUTO_SCORE_MIN", "52"))         # más bajo = más trades
AUTO_RSI_MIN = float(os.getenv("AUTO_RSI_MIN", "28"))           # más agresivo
AUTO_RSI_MAX = float(os.getenv("AUTO_RSI_MAX", "78"))
AUTO_ATR_PCT_MIN = float(os.getenv("AUTO_ATR_PCT_MIN", "0.12")) # más permisivo
AUTO_VOL_MULT_MIN = float(os.getenv("AUTO_VOL_MULT_MIN", "1.15"))
AUTO_MIN_QUOTE_VOL_5M = float(os.getenv("AUTO_MIN_QUOTE_VOL_5M", "120000"))  # liquidez mínima

# Trading rules
MAX_TRADES_DAY = int(os.getenv("MAX_TRADES_DAY", "25"))
SIGNAL_TTL_SEC = int(os.getenv("SIGNAL_TTL_SEC", str(12 * 60)))  # 12 min
PRICE_GUARD_PCT = float(os.getenv("PRICE_GUARD_PCT", "0.60"))     # más permisivo que 0.4%

SOFT_TARGET_PCT = float(os.getenv("SOFT_TARGET_PCT", "2.0"))     # desde +2% baja riesgo
HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", "-2.0"))        # -2% se apaga

POLL_SLEEP_SEC = float(os.getenv("POLL_SLEEP_SEC", "2"))

# =========================
# RUNTIME STATE
# =========================
state_lock = threading.Lock()
APP_LOOP: Optional[asyncio.AbstractEventLoop] = None

STATE: Dict = {
    "armed": False,
    "armed_until": 0,
    "capital": 0.0,
    "slots": 0,
    "used_capital": 0.0,
    "trades_today": 0,
    "pnl_today": 0.0,       # % (solo real cuando tengas ventas; por ahora informativo)
    "active_symbols": set(),
    "exec_lock": False,
    "day": None,
}

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    c = db()
    cur = c.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS trade_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT,
      side TEXT,
      usdt REAL,
      price REAL,
      qty REAL,
      pnl REAL,
      ts INTEGER
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_stats(
      day TEXT PRIMARY KEY,
      pnl REAL,
      trades INTEGER
    )""")

    # (si BOT1 ya lo creó, no pasa nada)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals_active(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL,
      side TEXT NOT NULL,
      signal_price REAL,
      confidence TEXT,
      score INTEGER,
      rsi REAL,
      atr_pct REAL,
      quote_vol_5m REAL,
      vol_mult REAL,
      active_mode TEXT,
      ladder TEXT,
      sl_pct REAL,
      note TEXT,
      status TEXT NOT NULL DEFAULT 'NEW',
      ts INTEGER NOT NULL,
      exec_ts INTEGER,
      last_error TEXT
    )""")

    c.commit()
    c.close()

# =========================
# BINANCE
# =========================
_session = requests.Session()
if BINANCE_API_KEY:
    _session.headers.update({"X-MBX-APIKEY": BINANCE_API_KEY})

def _sign(params: dict) -> dict:
    query = "&".join([f"{k}={params[k]}" for k in sorted(params)])
    sig = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def binance_get(path: str, params: dict = None) -> dict:
    r = _session.get(BINANCE_BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def binance_post(path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params = _sign(params)
    r = _session.post(BINANCE_BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def get_price(symbol: str) -> float:
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(data, columns=[
        "open_time","open","high","low","close","volume","close_time",
        "quote_vol","trades","taker_base","taker_quote","ignore"
    ])
    for col in ("open","high","low","close","volume","quote_vol"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna()

# =========================
# AUTO SIGNAL (agresivo)
# =========================
@dataclass
class AutoSig:
    symbol: str
    price: float
    ema20: float
    ema50: float
    rsi: float
    atr_pct: float
    vol_mult: float
    quote_vol_5m: float
    score: int
    ok: bool
    note: str

def rsi_series(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    ma_up = up.ewm(alpha=1/length, adjust=False).mean()
    ma_down = down.ewm(alpha=1/length, adjust=False).mean()
    rs = ma_up / (ma_down.replace(0, np.nan))
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)

def atr_series(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev = close.shift(1)
    tr = pd.concat([(high-low), (high-prev).abs(), (low-prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/length, adjust=False).mean()
    return atr.bfill()

def quote_vol_approx_5m(df: pd.DataFrame, interval: str) -> float:
    q = float(df["quote_vol"].iloc[-1])
    if interval.endswith("m"):
        mins = int(interval[:-1])
        return q * (5.0 / mins) if mins > 0 else q
    return q

def vol_mult(df: pd.DataFrame, lookback: int = 20) -> float:
    if len(df) < lookback + 2:
        return 1.0
    v_now = float(df["volume"].iloc[-1])
    v_avg = float(df["volume"].iloc[-(lookback+1):-1].mean())
    return 1.0 if v_avg <= 0 else (v_now / v_avg)

def auto_compute(symbol: str) -> AutoSig:
    df = fetch_klines(symbol, AUTO_INTERVAL, AUTO_LIMIT)
    close = df["close"]

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    rsi = rsi_series(close, 14)
    atr = atr_series(df, 14)

    price = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    r = float(rsi.iloc[-1])
    atr_pct = float((atr.iloc[-1] / price) * 100) if price > 0 else 0.0

    vm = vol_mult(df, 20)
    q5 = quote_vol_approx_5m(df, AUTO_INTERVAL)

    # Score agresivo (más fácil entrar)
    score = 0
    score += 18 if e20 >= e50 else 10                 # no castiga tanto bajista
    score += int(max(0, 25 - abs(r - 50) * 0.65))
    score += int(min(22, atr_pct * 30))
    score += int(min(20, max(0, (vm - 1.0) * 35)))
    score += 10 if q5 >= AUTO_MIN_QUOTE_VOL_5M else 0
    score = int(max(0, min(100, score)))

    ok_rsi = (AUTO_RSI_MIN <= r <= AUTO_RSI_MAX)
    ok_atr = (atr_pct >= AUTO_ATR_PCT_MIN)
    ok_vol = (vm >= AUTO_VOL_MULT_MIN and q5 >= AUTO_MIN_QUOTE_VOL_5M)
    ok_score = (score >= AUTO_SCORE_MIN)

    ok = ok_rsi and ok_atr and ok_vol and ok_score
    note = f"auto ok={ok} | score={score} rsi={r:.1f} atr%={atr_pct:.3f} vm={vm:.2f} q5={q5:.0f}"

    return AutoSig(symbol, price, e20, e50, r, atr_pct, vm, q5, score, ok, note)

def load_coins() -> List[str]:
    with open(COINS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        coins = data
    elif isinstance(data, dict) and isinstance(data.get("coins"), list):
        coins = data["coins"]
    else:
        raise ValueError("coins.json inválido")
    out = []
    for c in coins:
        s = str(c).upper().strip().replace("/", "")
        if s:
            out.append(s)
    return out

# =========================
# TELEGRAM SECURITY
# =========================
def _authorized(update: Update) -> bool:
    if not (AUTHORIZED_TELEGRAM_USER_ID and TELEGRAM_CHAT_ID):
        return False
    try:
        uid_ok = int(update.effective_user.id) == int(AUTHORIZED_TELEGRAM_USER_ID)
        chat_ok = int(update.effective_chat.id) == int(TELEGRAM_CHAT_ID)
        return uid_ok and chat_ok
    except Exception:
        return False

def send_from_thread(app, text: str) -> None:
    global APP_LOOP
    if APP_LOOP is None:
        return
    fut = asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text),
        APP_LOOP
    )
    try:
        fut.result(timeout=10)
    except Exception:
        pass

async def send_async(ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await ctx.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

# =========================
# COMMANDS
# =========================
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await send_async(ctx,
        "🧠 BOT2 AUTO (agresivo)\n"
        "/status\n"
        "/start_auto <capital> <slots> <minutos> <PIN>\n"
        "/stop_auto <PIN>\n"
        "/help\n\n"
        "Ej: /start_auto 40 2 180 1234"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    with state_lock:
        armed = STATE["armed"]
        until = STATE["armed_until"]
        cap = STATE["capital"]
        slots = STATE["slots"]
        used = STATE["used_capital"]
        trades = STATE["trades_today"]
        pnl = STATE["pnl_today"]
        day = STATE["day"]
    left = max(0, int(until - time.time())) if armed else 0
    await send_async(ctx,
        "📊 STATUS | BOT2 AUTO\n"
        f"ARMED: {armed} | Tiempo restante: {left}s\n"
        f"Capital: {cap:.2f} | Slots: {slots} | Usado: {used:.2f}\n"
        f"Trades hoy: {trades}/{MAX_TRADES_DAY} | PnL hoy: {pnl:.2f}% | Día: {day}\n"
        f"AUTO: score>={AUTO_SCORE_MIN} rsi={AUTO_RSI_MIN}-{AUTO_RSI_MAX} atr>={AUTO_ATR_PCT_MIN}%\n"
        f"vol>={AUTO_VOL_MULT_MIN} q5>={AUTO_MIN_QUOTE_VOL_5M:.0f} | TTL={SIGNAL_TTL_SEC}s | Guard={PRICE_GUARD_PCT:.2f}%\n"
        f"HardStop: {HARD_STOP_PCT:.2f}% | SoftTarget: +{SOFT_TARGET_PCT:.2f}%"
    )

async def cmd_start_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if len(ctx.args) < 4:
        await send_async(ctx, "Uso: /start_auto <capital> <slots> <minutos> <PIN>")
        return
    try:
        capital = float(ctx.args[0]); slots = int(ctx.args[1]); minutes = int(ctx.args[2]); pin = str(ctx.args[3]).strip()
    except Exception:
        await send_async(ctx, "❌ Formato inválido. Ej: /start_auto 40 2 180 1234")
        return
    if pin != BOT_PIN:
        await send_async(ctx, "❌ PIN incorrecto")
        return
    if capital < 10:
        await send_async(ctx, "❌ Capital muy bajo (mínimo 10 USDT).")
        return
    if slots < 1 or slots > 10:
        await send_async(ctx, "❌ Slots inválidos (1-10).")
        return
    if minutes < 1 or minutes > 24*60:
        await send_async(ctx, "❌ Minutos inválidos (1-1440).")
        return

    with state_lock:
        STATE["armed"] = True
        STATE["armed_until"] = int(time.time()) + minutes * 60
        STATE["capital"] = capital
        STATE["slots"] = slots
        STATE["used_capital"] = 0.0
        STATE["trades_today"] = 0
        STATE["pnl_today"] = 0.0
        STATE["active_symbols"].clear()
        STATE["exec_lock"] = False
        STATE["day"] = time.strftime("%Y-%m-%d")

    await send_async(ctx,
        "🧠 AUTO TRADING ACTIVADO\n"
        f"Capital: {capital:.2f} USDT\n"
        f"Slots: {slots}\n"
        f"Tiempo: {minutes} min\n"
        f"Perfil AUTO (agresivo): score>={AUTO_SCORE_MIN} | RSI {AUTO_RSI_MIN}-{AUTO_RSI_MAX}\n"
        f"ATR>={AUTO_ATR_PCT_MIN}% | VolMult>={AUTO_VOL_MULT_MIN} | Q5>={AUTO_MIN_QUOTE_VOL_5M:.0f}\n"
        f"TTL: {SIGNAL_TTL_SEC}s | Price guard: {PRICE_GUARD_PCT:.2f}%\n"
        f"Protección: {HARD_STOP_PCT}% | Reduce riesgo desde +{SOFT_TARGET_PCT}%"
    )

async def cmd_stop_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if len(ctx.args) < 1:
        await send_async(ctx, "Uso: /stop_auto <PIN>")
        return
    if str(ctx.args[0]).strip() != BOT_PIN:
        await send_async(ctx, "❌ PIN incorrecto")
        return
    with state_lock:
        STATE["armed"] = False
        STATE["armed_until"] = 0
        STATE["exec_lock"] = False
        STATE["active_symbols"].clear()
    await send_async(ctx, "🛑 AUTO TRADING DESACTIVADO")

# =========================
# EXECUTE BUY
# =========================
def execute_buy(app, symbol: str, usdt: float, signal_price: float, tag: str):
    price_now = get_price(symbol)

    if signal_price and signal_price > 0:
        diff = abs(price_now - signal_price) / signal_price * 100
        if diff > PRICE_GUARD_PCT:
            raise RuntimeError(f"Late entry (guard {diff:.2f}%)")

    order = binance_post("/api/v3/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": round(usdt, 2)
    })

    send_from_thread(app,
        "🚀 ORDEN EJECUTADA\n"
        f"{symbol}\n"
        f"USDT: {usdt:.2f}\n"
        f"Precio: {price_now:.8f}\n"
        f"Fuente: {tag}"
    )
    return order, price_now

# =========================
# CORE LOOP (THREAD)
# =========================
def process_loop(app):
    coins = []
    try:
        coins = load_coins()
    except Exception:
        pass

    last_auto_scan = 0

    while True:
        try:
            now = int(time.time())
            day = time.strftime("%Y-%m-%d")

            with state_lock:
                if STATE["day"] != day:
                    STATE["day"] = day
                    STATE["trades_today"] = 0
                    STATE["pnl_today"] = 0.0

                if not STATE["armed"] or now > STATE["armed_until"]:
                    STATE["armed"] = False
                    time.sleep(5)
                    continue

                if STATE["pnl_today"] <= HARD_STOP_PCT:
                    STATE["armed"] = False
                    send_from_thread(app, f"🧯 PROTECCIÓN DIARIA ACTIVADA ({STATE['pnl_today']:.2f}%)")
                    time.sleep(5)
                    continue

                if STATE["trades_today"] >= MAX_TRADES_DAY:
                    time.sleep(5)
                    continue

                if STATE["exec_lock"]:
                    time.sleep(1)
                    continue

            # ========== 1) Consumir señales BOT1 (signals_active NEW) ==========
            conn = db()
            rows = conn.execute("""
              SELECT * FROM signals_active
              WHERE status='NEW'
              ORDER BY ts ASC
              LIMIT 5
            """).fetchall()
            conn.close()

            did_trade = False

            for sig in rows:
                age = now - int(sig["ts"])
                if age > SIGNAL_TTL_SEC:
                    c = db()
                    c.execute("UPDATE signals_active SET status='EXPIRED' WHERE id=?", (sig["id"],))
                    c.commit(); c.close()
                    continue

                symbol = sig["symbol"]

                with state_lock:
                    if symbol in STATE["active_symbols"]:
                        continue
                    if STATE["slots"] > 0 and len(STATE["active_symbols"]) >= STATE["slots"]:
                        continue
                    STATE["exec_lock"] = True
                    STATE["active_symbols"].add(symbol)

                try:
                    with state_lock:
                        base = STATE["capital"] / max(1, STATE["slots"])
                        if STATE["pnl_today"] >= SOFT_TARGET_PCT:
                            base *= 0.5
                        usdt_avail = max(0.0, STATE["capital"] - STATE["used_capital"])
                        usdt = min(base, usdt_avail)

                    if usdt < 10:
                        raise RuntimeError("Capital insuficiente (min 10 USDT)")

                    execute_buy(app, symbol, usdt, float(sig["signal_price"] or 0), "BOT1")

                    c = db()
                    c.execute("UPDATE signals_active SET status='EXECUTED', exec_ts=? WHERE id=?", (int(time.time()), sig["id"]))
                    c.commit(); c.close()

                    with state_lock:
                        STATE["used_capital"] += usdt
                        STATE["trades_today"] += 1
                    did_trade = True

                except Exception as e:
                    c = db()
                    c.execute("UPDATE signals_active SET status='REJECTED', last_error=? WHERE id=?", (str(e), sig["id"]))
                    c.commit(); c.close()
                    send_from_thread(app, f"❌ RECHAZADA {symbol}: {e}")

                finally:
                    with state_lock:
                        STATE["exec_lock"] = False
                        STATE["active_symbols"].discard(symbol)

            # ========== 2) AUTO SCAN (agresivo) si no hubo trade y cada ~10s ==========
            if not did_trade and coins and (time.time() - last_auto_scan) > 10:
                last_auto_scan = time.time()

                # escaneo rápido (máx 5 monedas por vuelta para no saturar)
                sample = coins[:]
                # rotación simple
                sample = sample[(now % max(1, len(sample))):] + sample[:(now % max(1, len(sample)))]

                checked = 0
                for symbol in sample:
                    if checked >= 5:
                        break
                    checked += 1

                    with state_lock:
                        if symbol in STATE["active_symbols"]:
                            continue
                        if STATE["slots"] > 0 and len(STATE["active_symbols"]) >= STATE["slots"]:
                            break
                        if STATE["exec_lock"]:
                            break
                        STATE["exec_lock"] = True
                        STATE["active_symbols"].add(symbol)

                    try:
                        a = auto_compute(symbol)
                        if not a.ok:
                            continue

                        with state_lock:
                            base = STATE["capital"] / max(1, STATE["slots"])
                            if STATE["pnl_today"] >= SOFT_TARGET_PCT:
                                base *= 0.5
                            usdt_avail = max(0.0, STATE["capital"] - STATE["used_capital"])
                            usdt = min(base, usdt_avail)

                        if usdt < 10:
                            raise RuntimeError("Capital insuficiente (min 10 USDT)")

                        execute_buy(app, symbol, usdt, a.price, "AUTO")
                        with state_lock:
                            STATE["used_capital"] += usdt
                            STATE["trades_today"] += 1

                        # guarda log mínimo
                        c = db()
                        c.execute(
                            "INSERT INTO trade_log(symbol,side,usdt,price,qty,pnl,ts) VALUES(?,?,?,?,?,?,?)",
                            (symbol, "BUY", float(usdt), float(a.price), 0.0, 0.0, int(time.time()))
                        )
                        c.commit(); c.close()

                        send_from_thread(app, f"🧠 AUTO-ENTRY OK: {symbol}\n{a.note}")
                        did_trade = True

                    except Exception as e:
                        send_from_thread(app, f"❌ AUTO rechazado {symbol}: {e}")

                    finally:
                        with state_lock:
                            STATE["exec_lock"] = False
                            STATE["active_symbols"].discard(symbol)

            time.sleep(POLL_SLEEP_SEC)

        except Exception:
            traceback.print_exc()
            time.sleep(5)

# =========================
# MAIN
# =========================
async def post_init(app):
    global APP_LOOP
    APP_LOOP = asyncio.get_running_loop()
    db_init()
    t = threading.Thread(target=process_loop, args=(app,), daemon=True)
    t.start()
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🤖 BOT2 AUTO listo. Usa /start_auto")
    except Exception:
        pass

def main():
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and AUTHORIZED_TELEGRAM_USER_ID):
        print("❌ Faltan variables TELEGRAM_*_AUTO o AUTHORIZED_TELEGRAM_USER_ID.")
        return
    if not (BINANCE_API_KEY and BINANCE_API_SECRET):
        print("❌ Faltan BINANCE_API_KEY / BINANCE_API_SECRET.")
        return
    if not BOT_PIN:
        print("❌ BOT_PIN no configurado.")
        return

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("start_auto", cmd_start_auto))
    app.add_handler(CommandHandler("stop_auto", cmd_stop_auto))

    print("✅ Bot2 AUTO (agresivo) iniciado. Polling ON.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
