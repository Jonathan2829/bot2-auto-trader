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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
import pandas as pd

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# =========================
# PARÁMETROS AUTO
# =========================
POLL_SLEEP_SEC = float(os.getenv("POLL_SLEEP_SEC", "10"))

# Guardia de “no entrar tarde” vs señal BOT1
PRICE_GUARD_PCT = float(os.getenv("PRICE_GUARD_PCT", "0.60"))  # 0.60%

# Gestión riesgo simple
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "20"))
MIN_USDT_PER_TRADE = float(os.getenv("MIN_USDT_PER_TRADE", "10"))

# “Protección diaria” (placeholders; si luego implementas ventas, ahí sí cobran sentido)
HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", "2.0"))       # -2%
SOFT_TARGET_PCT = float(os.getenv("SOFT_TARGET_PCT", "2.0"))   # +2% reduce riesgo

# =========================
# STATE
# =========================
state_lock = threading.Lock()
STATE = {
    "armed": False,
    "armed_until": 0,
    "capital": float(os.getenv("CAPITAL_USDT", "300")),  # capital lógico usado por el bot
    "slots": int(os.getenv("SLOTS", "3")),
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
    CREATE TABLE IF NOT EXISTS daily_state(
      day TEXT PRIMARY KEY,
      trades_today INTEGER,
      pnl_today REAL,
      used_capital REAL
    )""")

    # señales que “produce” BOT1 (si ya tienes esa tabla en BOT1, perfecto)
    # Campos mínimos que este BOT2 usa: id, symbol, signal_price, status, ts
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals_active(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT,
      signal_price REAL,
      status TEXT DEFAULT 'NEW',
      ts INTEGER,
      exec_ts INTEGER,
      last_error TEXT
    )""")

    c.commit()
    c.close()

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def load_daily_state():
    day = _today_key()
    with state_lock:
        STATE["day"] = day
    c = db()
    row = c.execute("SELECT day, trades_today, pnl_today, used_capital FROM daily_state WHERE day=?", (day,)).fetchone()
    if row:
        with state_lock:
            STATE["trades_today"] = int(row["trades_today"] or 0)
            STATE["pnl_today"] = float(row["pnl_today"] or 0.0)
            STATE["used_capital"] = float(row["used_capital"] or 0.0)
    else:
        c.execute("INSERT OR REPLACE INTO daily_state(day, trades_today, pnl_today, used_capital) VALUES(?,?,?,?)",
                  (day, 0, 0.0, 0.0))
        c.commit()
    c.close()

def save_daily_state():
    with state_lock:
        day = STATE["day"] or _today_key()
        t = STATE["trades_today"]
        pnl = STATE["pnl_today"]
        used = STATE["used_capital"]
    c = db()
    c.execute("INSERT OR REPLACE INTO daily_state(day, trades_today, pnl_today, used_capital) VALUES(?,?,?,?)",
              (day, t, pnl, used))
    c.commit()
    c.close()

# =========================
# TELEGRAM HELPERS
# =========================
def _authorized(update: Update) -> bool:
    if not AUTHORIZED_TELEGRAM_USER_ID:
        return True
    try:
        uid = str(update.effective_user.id)
        return uid == str(AUTHORIZED_TELEGRAM_USER_ID).strip()
    except Exception:
        return False

async def send_async(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    if TELEGRAM_CHAT_ID:
        await ctx.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

def send_from_thread(app: Application, text: str):
    # Enviar desde thread al chat AUTO
    try:
        if TELEGRAM_CHAT_ID:
            app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception:
        pass

# =========================
# BINANCE (ROBUSTO: firma + errores + MARKET por quote/qty)
# =========================
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

_session = requests.Session()
if BINANCE_API_KEY:
    _session.headers.update({"X-MBX-APIKEY": BINANCE_API_KEY})

_exchange_cache = {}

def _build_query(params: dict) -> str:
    return urlencode(params, doseq=True)

def _sign_query(query: str) -> str:
    return hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def _raise_binance(r: requests.Response) -> dict:
    """
    NO uses r.raise_for_status(): te tapa el code/msg real de Binance.
    """
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if not r.ok:
        code = data.get("code")
        msg = data.get("msg")
        raise RuntimeError(f"Binance HTTP {r.status_code} | code={code} | msg={msg}")
    return data

def binance_get(path: str, params: dict = None) -> dict:
    r = _session.get(BINANCE_BASE + path, params=params, timeout=15)
    return _raise_binance(r)

def binance_post_signed(path: str, params: dict) -> dict:
    """
    POST signed: manda params en CUERPO (data) y firma exactamente ese query-string.
    """
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)

    query = _build_query(params)
    sig = _sign_query(query)

    r = _session.post(
        BINANCE_BASE + path,
        data=query + "&signature=" + sig,
        timeout=15
    )
    return _raise_binance(r)

def get_price(symbol: str) -> float:
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

def _get_symbol_rules(symbol: str) -> dict:
    if symbol in _exchange_cache:
        return _exchange_cache[symbol]
    info = binance_get("/api/v3/exchangeInfo", {"symbol": symbol})
    s = info["symbols"][0]

    lot = next(f for f in s["filters"] if f["filterType"] == "LOT_SIZE")
    step = Decimal(lot["stepSize"])
    min_qty = Decimal(lot["minQty"])

    notional = next((f for f in s["filters"] if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL")), None)
    min_notional = Decimal(notional.get("minNotional", "0")) if notional else Decimal("0")

    rules = {
        "quoteOrderQtyMarketAllowed": bool(s.get("quoteOrderQtyMarketAllowed", True)),
        "stepSize": step,
        "minQty": min_qty,
        "minNotional": min_notional,
    }
    _exchange_cache[symbol] = rules
    return rules

def _round_step(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step

def market_buy_usdt(symbol: str, usdt: float):
    """
    Compra MARKET con USDT.
    - Si el símbolo permite quoteOrderQtyMarketAllowed=True: usa quoteOrderQty
    - Si no: calcula quantity con precio actual y redondea al stepSize
    Retorna: (order_json, price_now, qty_estimada)
    """
    rules = _get_symbol_rules(symbol)
    price_now = get_price(symbol)

    usdt_d = Decimal(str(usdt))
    if rules["quoteOrderQtyMarketAllowed"]:
        order = binance_post_signed("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": str(usdt_d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        })
        return order, price_now, 0.0

    qty = usdt_d / Decimal(str(price_now))
    qty = _round_step(qty, rules["stepSize"])

    if qty < rules["minQty"]:
        raise RuntimeError(f"{symbol}: qty {qty} < minQty {rules['minQty']}")

    order = binance_post_signed("/api/v3/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": format(qty, "f"),
    })
    return order, price_now, float(qty)

# =========================
# TA / ENTRIES (simple)
# =========================
def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    rows = []
    for k in data:
        rows.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    df = pd.DataFrame(rows)
    return df

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    return 100 - (100 / (1 + rs))

@dataclass
class AutoEntry:
    symbol: str
    note: str
    score: float
    price: float

def load_coins() -> List[str]:
    try:
        with open(COINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "coins" in data:
            return [str(x).strip().upper() for x in data["coins"] if str(x).strip()]
        if isinstance(data, list):
            return [str(x).strip().upper() for x in data if str(x).strip()]
    except Exception:
        pass
    return []

def compute_auto_entry(symbol: str) -> Optional[AutoEntry]:
    """
    Lógica agresiva simple:
    - toma 15m (150 velas)
    - busca pullback: close < EMA20 pero EMA20 > EMA50 (tendencia suave alcista)
    - RSI entre 35-55 para “rebote”
    """
    try:
        df = fetch_klines(symbol, "15m", 150)
        c = df["close"]
        e20 = ema(c, 20)
        e50 = ema(c, 50)
        r = rsi(c, 14)

        close = float(c.iloc[-1])
        ema20 = float(e20.iloc[-1])
        ema50 = float(e50.iloc[-1])
        rsi14 = float(r.iloc[-1])

        trend_ok = ema20 > ema50
        pullback_ok = close < ema20
        rsi_ok = (35 <= rsi14 <= 55)

        score = 0.0
        note_parts = []
        if trend_ok:
            score += 1.0; note_parts.append("EMA20>EMA50")
        if pullback_ok:
            score += 1.0; note_parts.append("pullback <EMA20")
        if rsi_ok:
            score += 1.0; note_parts.append(f"RSI={rsi14:.1f}")

        if score >= 2.0:
            return AutoEntry(symbol=symbol, note=" | ".join(note_parts), score=score, price=close)
        return None
    except Exception:
        return None

# =========================
# TELEGRAM COMMANDS
# =========================
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    with state_lock:
        armed = STATE["armed"]
        until = STATE["armed_until"]
        cap = STATE["capital"]
        used = STATE["used_capital"]
        slots = STATE["slots"]
        t = STATE["trades_today"]
        pnl = STATE["pnl_today"]
    txt = (
        "📊 STATUS | BOT2 AUTO\n"
        f"🧠 ARMADO: {armed} (hasta {until})\n"
        f"💰 Capital: {cap:.2f} | Usado: {used:.2f} | Slots: {slots}\n"
        f"🔁 Trades hoy: {t}/{MAX_TRADES_PER_DAY} | PnL hoy: {pnl:.2f}%\n"
        f"🛡 Protección: {HARD_STOP_PCT}% | Reduce riesgo desde +{SOFT_TARGET_PCT}%"
    )
    await send_async(ctx, txt)

async def cmd_arm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if len(ctx.args) < 2:
        await send_async(ctx, "Uso: /arm <MINUTOS> <PIN>")
        return
    minutes = int(str(ctx.args[0]).strip())
    pin = str(ctx.args[1]).strip()
    if pin != BOT_PIN:
        await send_async(ctx, "❌ PIN incorrecto")
        return
    until = int(time.time()) + minutes * 60
    with state_lock:
        STATE["armed"] = True
        STATE["armed_until"] = until
    await send_async(ctx, f"✅ AUTO ARMADO por {minutes} min (hasta {until})")

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
    await send_async(ctx, "🛑 AUTO detenido")

# =========================
# CORE BUY
# =========================
def execute_buy(app, symbol: str, usdt: float, signal_price: float, tag: str):
    # Guardia para no entrar tarde si la señal ya se movió demasiado
    price_now = get_price(symbol)
    if signal_price and signal_price > 0:
        diff = abs(price_now - signal_price) / signal_price * 100
        if diff > PRICE_GUARD_PCT:
            raise RuntimeError(f"Late entry (guard {diff:.2f}%)")

    order, price_exec, _qty_est = market_buy_usdt(symbol, usdt)

    send_from_thread(
        app,
        "🚀 ORDEN EJECUTADA\n"
        f"{symbol}\n"
        f"USDT: {usdt:.2f}\n"
        f"Precio: {price_exec:.8f}\n"
        f"Fuente: {tag}"
    )
    return order, price_exec

# =========================
# CORE LOOP (THREAD)
# =========================
def process_loop(app: Application):
    coins = load_coins()
    send_from_thread(app, f"✅ BOT2 AUTO loop ON | coins={len(coins)} | check={POLL_SLEEP_SEC}s")

    while True:
        try:
            # reset diario
            today = _today_key()
            with state_lock:
                if STATE["day"] != today:
                    STATE["day"] = today
                    STATE["trades_today"] = 0
                    STATE["pnl_today"] = 0.0
                    STATE["used_capital"] = 0.0
                    STATE["active_symbols"] = set()
                    save_daily_state()

            with state_lock:
                armed = STATE["armed"]
                until = STATE["armed_until"]
                trades_today = STATE["trades_today"]
                used_capital = STATE["used_capital"]
                cap = STATE["capital"]

            if armed and int(time.time()) > int(until):
                with state_lock:
                    STATE["armed"] = False
                    STATE["armed_until"] = 0
                armed = False

            if (not armed) or trades_today >= MAX_TRADES_PER_DAY:
                time.sleep(POLL_SLEEP_SEC)
                continue

            # 1) Consumir señales NEW de BOT1
            did_trade = False
            c = db()
            sigs = c.execute(
                "SELECT id, symbol, signal_price, ts, status FROM signals_active "
                "WHERE status='NEW' ORDER BY ts ASC LIMIT 3"
            ).fetchall()
            c.close()

            for sig in sigs:
                symbol = str(sig["symbol"]).upper().strip()
                if not symbol:
                    continue

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

                    if usdt < MIN_USDT_PER_TRADE:
                        raise RuntimeError(f"Capital insuficiente (min {MIN_USDT_PER_TRADE} USDT)")

                    execute_buy(app, symbol, usdt, float(sig["signal_price"] or 0), "BOT1")

                    c2 = db()
                    c2.execute(
                        "UPDATE signals_active SET status='EXECUTED', exec_ts=? WHERE id=?",
                        (int(time.time()), sig["id"])
                    )
                    c2.commit()
                    c2.close()

                    with state_lock:
                        STATE["used_capital"] += usdt
                        STATE["trades_today"] += 1
                    save_daily_state()
                    did_trade = True

                except Exception as e:
                    # guardar error real
                    try:
                        c2 = db()
                        c2.execute(
                            "UPDATE signals_active SET status='REJECTED', last_error=? WHERE id=?",
                            (str(e), sig["id"])
                        )
                        c2.commit()
                        c2.close()
                    except Exception:
                        pass
                    send_from_thread(app, f"❌ AUTO rechazado {symbol}: {e}")

                finally:
                    with state_lock:
                        STATE["exec_lock"] = False
                        STATE["active_symbols"].discard(symbol)

            if did_trade:
                time.sleep(POLL_SLEEP_SEC)
                continue

            # 2) Si no hubo señales, buscar auto-entries (agresivo)
            #    (limitado a 1 trade por vuelta)
            for symbol in coins[:]:
                with state_lock:
                    trades_today = STATE["trades_today"]
                    if trades_today >= MAX_TRADES_PER_DAY:
                        break
                    if symbol in STATE["active_symbols"]:
                        continue
                    if STATE["slots"] > 0 and len(STATE["active_symbols"]) >= STATE["slots"]:
                        continue
                    STATE["active_symbols"].add(symbol)

                try:
                    a = compute_auto_entry(symbol)
                    if not a:
                        continue

                    with state_lock:
                        base = STATE["capital"] / max(1, STATE["slots"])
                        if STATE["pnl_today"] >= SOFT_TARGET_PCT:
                            base *= 0.5
                        usdt_avail = max(0.0, STATE["capital"] - STATE["used_capital"])
                        usdt = min(base, usdt_avail)

                    if usdt < MIN_USDT_PER_TRADE:
                        continue

                    execute_buy(app, symbol, usdt, 0.0, "AUTO")

                    with state_lock:
                        STATE["used_capital"] += usdt
                        STATE["trades_today"] += 1
                    save_daily_state()

                    send_from_thread(app, f"🧠 AUTO-ENTRY OK: {symbol}\n{a.note}")
                    break

                except Exception as e:
                    send_from_thread(app, f"❌ AUTO rechazado {symbol}: {e}")

                finally:
                    with state_lock:
                        STATE["active_symbols"].discard(symbol)

            time.sleep(POLL_SLEEP_SEC)

        except Exception:
            send_from_thread(app, "⚠️ Loop error:\n" + traceback.format_exc())
            time.sleep(5)

# =========================
# MAIN
# =========================
def main():
    db_init()
    load_daily_state()

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Falta TELEGRAM_TOKEN_AUTO o TELEGRAM_CHAT_ID_AUTO")
        return
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("❌ Falta BINANCE_API_KEY o BINANCE_API_SECRET")
        return
    if not BOT_PIN:
        print("❌ Falta BOT_PIN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("arm", cmd_arm))
    app.add_handler(CommandHandler("stop_auto", cmd_stop_auto))

    t = threading.Thread(target=process_loop, args=(app,), daemon=True)
    t.start()

    print("✅ BOT2 AUTO iniciado.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
