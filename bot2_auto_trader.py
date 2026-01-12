# bot2_auto_trader.py
# BOT2 PAPER TRADER (agresivo si quieres) + atado a BOT1 por:
#  - SIGNAL_SOURCE=sqlite   (lee signals_active de un sqlite compartido)
#  - SIGNAL_SOURCE=telegram (BOT1 reenvía señal al chat de BOT2 como "SIG ...")
#
# Importante:
# - NO manda órdenes reales. Solo PAPER (simulación con precio real ticker/price).
# - Mantiene estados en SQLite (positions/kv_state/kv_runtime) usando tu db_sqlite.py

import os
import time
import json
import math
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

import requests
from decimal import Decimal

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from db_sqlite import BotDB
from mode_profiles import get_profile

# =========================
# ENV
# =========================
TELEGRAM_TOKEN_AUTO = os.getenv("TELEGRAM_TOKEN_AUTO", "").strip()
TELEGRAM_CHAT_ID_AUTO = os.getenv("TELEGRAM_CHAT_ID_AUTO", "").strip()
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "").strip()

# BOT2 DB (posiciones paper del bot2)
SQLITE_FILE = os.getenv("SQLITE_FILE", "bot_state.sqlite3")

# Fuente de señales
SIGNAL_SOURCE = os.getenv("SIGNAL_SOURCE", "telegram").strip().lower()  # telegram | sqlite
# Si SIGNAL_SOURCE=sqlite, este es el sqlite donde BOT1 escribe signals_active
SIGNALS_SQLITE_FILE = os.getenv("SIGNALS_SQLITE_FILE", SQLITE_FILE)

# Paper params
PAPER_FEE = float(os.getenv("PAPER_FEE", "0.001"))              # 0.10%
PAPER_SLIPPAGE_BPS = float(os.getenv("PAPER_SLIPPAGE_BPS", "2"))# 2 bps = 0.02%
LOOP_SEC = int(os.getenv("LOOP_SEC", "10"))

# Modo
MODE = os.getenv("MODE", "AGRESIVO").upper().strip()

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.com").rstrip("/")

# =========================
# Helpers
# =========================
def now_ms() -> int:
    return int(time.time() * 1000)

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def authorized(update: Update) -> bool:
    if not AUTHORIZED_TELEGRAM_USER_ID:
        return True
    try:
        uid = str(update.effective_user.id)
        return uid == str(AUTHORIZED_TELEGRAM_USER_ID).strip()
    except Exception:
        return False

def get_last_price(symbol: str) -> float:
    r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=12)
    r.raise_for_status()
    return float(r.json()["price"])

def apply_slippage(price: float, side: str) -> float:
    slip = PAPER_SLIPPAGE_BPS / 10000.0
    if side.upper() == "BUY":
        return price * (1.0 + slip)
    return price * (1.0 - slip)

def parse_sig_message(text: str) -> Optional[Dict[str, Any]]:
    """
    Formato recomendado (simple y robusto):
      SIG SYMBOL USD ENTRY LADDER SL
    Ej:
      SIG ADAUSDT 20 0.403 2:50,3.5:30,5:20 2.0

    También acepta JSON:
      {"type":"SIG","symbol":"ADAUSDT","usd":20,"entry":0.403,"ladder":"2:50,3.5:30,5:20","sl":2.0}
    """
    t = (text or "").strip()
    if not t:
        return None

    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
            if str(obj.get("type", "")).upper() in ("SIG", "SIGNAL"):
                return {
                    "symbol": str(obj.get("symbol", "")).upper().strip(),
                    "usd": safe_float(obj.get("usd", 0)),
                    "entry": safe_float(obj.get("entry", 0)),
                    "ladder": str(obj.get("ladder", "")).strip(),
                    "sl": safe_float(obj.get("sl", 0)),
                    "ts": now_ms(),
                    "source": "telegram_json",
                }
        except Exception:
            return None

    parts = t.split()
    if len(parts) >= 6 and parts[0].upper() == "SIG":
        return {
            "symbol": parts[1].upper().strip(),
            "usd": safe_float(parts[2], 0),
            "entry": safe_float(parts[3], 0),
            "ladder": parts[4].strip(),
            "sl": safe_float(parts[5], 0),
            "ts": now_ms(),
            "source": "telegram_text",
        }
    return None

def ladder_to_hits(ladder: str) -> Dict[str, int]:
    """
    Guarda hits por nivel, ej {"2.0":0,"3.5":0,"5.0":0}
    """
    hits = {}
    for part in (ladder or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p, _w = part.split(":")
            hits[str(float(p))] = 0
        except Exception:
            pass
    return hits

# =========================
# Core paper execution
# =========================
@dataclass
class PaperOrder:
    ok: bool
    side: str
    symbol: str
    qty: float
    price: float
    fee: float
    err: str = ""

def paper_buy(symbol: str, usd: float) -> PaperOrder:
    try:
        px = get_last_price(symbol)
        fill = apply_slippage(px, "BUY")
        qty = usd / fill if fill > 0 else 0.0
        qty = math.floor(qty * 1e8) / 1e8
        fee = usd * PAPER_FEE
        if qty <= 0:
            return PaperOrder(False, "BUY", symbol, 0.0, fill, fee, "qty<=0")
        return PaperOrder(True, "BUY", symbol, qty, fill, fee, "")
    except Exception as e:
        return PaperOrder(False, "BUY", symbol, 0.0, 0.0, 0.0, str(e))

def paper_sell(symbol: str, qty: float) -> PaperOrder:
    try:
        px = get_last_price(symbol)
        fill = apply_slippage(px, "SELL")
        gross = qty * fill
        fee = gross * PAPER_FEE
        if qty <= 0:
            return PaperOrder(False, "SELL", symbol, 0.0, fill, fee, "qty<=0")
        return PaperOrder(True, "SELL", symbol, qty, fill, fee, "")
    except Exception as e:
        return PaperOrder(False, "SELL", symbol, 0.0, 0.0, 0.0, str(e))

def pct_gain(price: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    return (price / entry - 1.0) * 100.0

def parse_ladder(ladder: str) -> List[Tuple[float, float]]:
    # "2:50,3.5:30,5:20" => [(2.0,0.50),(3.5,0.30),(5.0,0.20)]
    out = []
    for part in (ladder or "").split(","):
        part = part.strip()
        if not part:
            continue
        p, w = part.split(":")
        out.append((float(p), float(w) / 100.0))
    s = sum(w for _, w in out)
    if s <= 0:
        return []
    return [(p, w / s) for p, w in out]

# =========================
# BOT
# =========================
class Bot2Paper:
    def __init__(self):
        self.db = BotDB(SQLITE_FILE)
        self.profile = get_profile(MODE)

        # runtime
        self.db.runtime_set("mode", MODE)
        self.db.runtime_set("signal_source", SIGNAL_SOURCE)
        self.last_action_ts = int(self.db.runtime_get("last_action_ts", 0) or 0)

    def can_trade(self) -> Tuple[bool, str]:
        # Puedes meter aquí tu "daily loss cap" si quieres (ya lo tienes en BOT1).
        # Por ahora: siempre OK.
        return True, "OK"

    def max_positions(self) -> int:
        return int(self.profile.get("MAX_POS", self.profile.get("max_positions", 3)) or 3)

    def per_trade_usd(self) -> float:
        return float(self.profile.get("USD_PER_TRADE", self.profile.get("per_trade_usdt", 20)) or 20)

    def sl_default(self) -> float:
        return float(self.profile.get("SL_PCT", self.profile.get("sl_pct", 2.4)) or 2.4)

    def ladder_default(self) -> str:
        return str(self.profile.get("LADDER", self.profile.get("ladder", "2.5:45,4:35,5.5:20")))

    def cooldown_sec(self) -> int:
        return int(self.profile.get("COOLDOWN", self.profile.get("cooldown_sec", 60)) or 60)

    def cooldown_ok(self) -> bool:
        return (time.time() - (self.last_action_ts / 1000.0)) >= self.cooldown_sec()

    def _touch_action(self):
        self.last_action_ts = now_ms()
        self.db.runtime_set("last_action_ts", self.last_action_ts)

    def status_text(self) -> str:
        pos = self.db.list_positions()
        return (
            f"📊 STATUS | BOT2 PAPER\n"
            f"🧠 Modo: {MODE}\n"
            f"🔗 Señales: {SIGNAL_SOURCE}\n"
            f"📌 Posiciones: {len(pos)}/{self.max_positions()}\n"
            f"⏱ Cooldown: {self.cooldown_sec()}s | Loop: {LOOP_SEC}s\n"
            f"💸 Fee: {PAPER_FEE*100:.3f}% | Slippage: {PAPER_SLIPPAGE_BPS} bps\n"
            f"🕒 {now_utc()}"
        )

    def paper_open_from_signal(self, sig: Dict[str, Any]) -> Tuple[bool, str]:
        ok, reason = self.can_trade()
        if not ok:
            return False, reason

        if not self.cooldown_ok():
            return False, f"Cooldown activo ({self.cooldown_sec()}s)"

        symbol = sig["symbol"]
        usd = float(sig.get("usd") or 0) or self.per_trade_usd()
        ladder = (sig.get("ladder") or "").strip() or self.ladder_default()
        sl = float(sig.get("sl") or 0) or self.sl_default()
        entry_hint = float(sig.get("entry") or 0)

        # límites
        positions = self.db.list_positions()
        if len(positions) >= self.max_positions():
            return False, f"Max posiciones alcanzado {len(positions)}/{self.max_positions()}"

        if self.db.get_position(symbol):
            return False, f"Ya existe posición en {symbol}"

        # compra paper
        buy = paper_buy(symbol, usd)
        if not buy.ok:
            return False, f"PAPER BUY falló: {buy.err}"

        # entry real paper = fill price (no el entry_hint)
        entry = buy.price
        qty = buy.qty
        buy_cost = usd

        # guarda posición en tu esquema
        self.db.upsert_position(
            symbol=symbol,
            in_position=1,
            usd=float(usd),
            entry=float(entry),
            qty=float(qty),
            buy_cost=float(buy_cost),
            fee_buy=float(buy.fee),
            fee_sell=0.0,
            sl_pct=float(sl),
            ladder_json=json.dumps({"ladder": ladder}),
            hits_json=json.dumps(ladder_to_hits(ladder)),
            be_sent=0,
            ts=now_ms()
        )

        self._touch_action()
        return True, (
            f"✅ PAPER BUY (signal)\n"
            f"{symbol} | usd={usd:.2f}\n"
            f"qty={qty:.8f} @ {entry:.6f}\n"
            f"fee≈{buy.fee:.4f} USDT\n"
            f"SL={sl:.2f}% | ladder={ladder}\n"
            f"hint_entry(BOT1)={entry_hint:.6f}"
        )

    def manage_positions_tp_sl(self) -> List[str]:
        notes = []
        for p in self.db.list_positions():
            try:
                symbol = p["symbol"]
                entry = float(p["entry"])
                qty = float(p["qty"])
                sl_pct = float(p["sl_pct"])
                ladder = json.loads(p["ladder_json"]).get("ladder", self.ladder_default())
                hits = json.loads(p["hits_json"]) if p["hits_json"] else ladder_to_hits(ladder)

                px = get_last_price(symbol)
                g = pct_gain(px, entry)

                # SL: vende todo
                if g <= -abs(sl_pct):
                    sell = paper_sell(symbol, qty)
                    if sell.ok:
                        self.db.delete_position(symbol)
                        notes.append(f"🧯 SL | {symbol} gain={g:.2f}% → SELL 100% @ {sell.price:.6f}")
                    else:
                        notes.append(f"⚠️ SL | {symbol} error sell: {sell.err}")
                    continue

                # TP ladder: por niveles (vende % de lo que queda una sola vez por nivel)
                for lvl, w in parse_ladder(ladder):
                    k = str(float(lvl))
                    if hits.get(k, 0) == 1:
                        continue
                    if g >= lvl:
                        sell_qty = qty * w
                        sell_qty = math.floor(sell_qty * 1e8) / 1e8
                        if sell_qty <= 0:
                            hits[k] = 1
                            continue

                        sell = paper_sell(symbol, sell_qty)
                        if sell.ok:
                            qty_new = qty - sell_qty
                            qty_new = max(0.0, qty_new)

                            hits[k] = 1
                            if qty_new <= 1e-10:
                                self.db.delete_position(symbol)
                                notes.append(f"🎯 TP | {symbol} lvl={lvl:.2f}% → SELL final @ {sell.price:.6f}")
                            else:
                                # actualiza qty y hits, fee_sell acumulada
                                fee_sell_total = float(p["fee_sell"]) + float(sell.fee)
                                self.db.update_position_fields(symbol, {
                                    "qty": float(qty_new),
                                    "fee_sell": float(fee_sell_total),
                                    "hits_json": json.dumps(hits),
                                    "ts": now_ms()
                                })
                                notes.append(f"🎯 TP | {symbol} lvl={lvl:.2f}% → SELL {w*100:.1f}% @ {sell.price:.6f} | qty_left={qty_new:.8f}")

                            # refresca qty base para niveles siguientes en el mismo loop
                            qty = qty_new
                        else:
                            notes.append(f"⚠️ TP | {symbol} error sell: {sell.err}")

            except Exception as e:
                notes.append(f"⚠️ manage err {p.get('symbol','?')}: {e}")
        return notes

    # =========================
    # Señales desde sqlite (BOT1)
    # =========================
    def fetch_signals_from_sqlite(self) -> List[Dict[str, Any]]:
        out = []
        try:
            import sqlite3
            conn = sqlite3.connect(SIGNALS_SQLITE_FILE)
            conn.row_factory = sqlite3.Row

            # asegúrate que exista
            conn.execute("""
            CREATE TABLE IF NOT EXISTS signals_active(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT,
              side TEXT,
              signal_price REAL,
              status TEXT DEFAULT 'NEW',
              ladder TEXT,
              sl_pct REAL,
              note TEXT,
              ts INTEGER,
              exec_ts INTEGER,
              last_error TEXT
            );
            """)
            conn.commit()

            rows = conn.execute(
                "SELECT id, symbol, signal_price, ladder, sl_pct, ts FROM signals_active WHERE status='NEW' ORDER BY id ASC LIMIT 5"
            ).fetchall()

            for r in rows:
                out.append({
                    "id": int(r["id"]),
                    "symbol": str(r["symbol"]).upper(),
                    "usd": self.per_trade_usd(),
                    "entry": float(r["signal_price"] or 0),
                    "ladder": str(r["ladder"] or self.ladder_default()),
                    "sl": float(r["sl_pct"] or self.sl_default()),
                    "ts": int(r["ts"] or now_ms()),
                    "source": "sqlite",
                })
            conn.close()
        except Exception:
            pass
        return out

    def mark_signal_sqlite(self, signal_id: int, status: str, err: str = ""):
        try:
            import sqlite3
            conn = sqlite3.connect(SIGNALS_SQLITE_FILE)
            if status == "EXECUTED":
                conn.execute("UPDATE signals_active SET status='EXECUTED', exec_ts=? WHERE id=?", (now_ms(), signal_id))
            else:
                conn.execute("UPDATE signals_active SET status='REJECTED', last_error=? WHERE id=?", (err[:300], signal_id))
            conn.commit()
            conn.close()
        except Exception:
            pass

# =========================
# Telegram handlers
# =========================
bot = Bot2Paper()

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(bot.status_text())

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    global MODE
    if not context.args:
        await update.message.reply_text(f"Modo actual: {MODE}")
        return
    MODE = context.args[0].upper().strip()
    bot.profile = get_profile(MODE)
    bot.db.runtime_set("mode", MODE)
    await update.message.reply_text(f"✅ Modo actualizado: {MODE}")

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /buy SYMBOL USD  (paper)")
        return
    symbol = context.args[0].upper().strip()
    usd = safe_float(context.args[1], bot.per_trade_usd())
    sig = {"symbol": symbol, "usd": usd, "entry": 0, "ladder": bot.ladder_default(), "sl": bot.sl_default(), "ts": now_ms(), "source": "manual"}
    ok, msg = bot.paper_open_from_signal(sig)
    await update.message.reply_text(("✅ " if ok else "❌ ") + msg)

async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /sell SYMBOL [PCT]")
        return
    symbol = context.args[0].upper().strip()
    pct = safe_float(context.args[1], 100.0) if len(context.args) >= 2 else 100.0
    p = bot.db.get_position(symbol)
    if not p:
        await update.message.reply_text(f"⛔ No hay posición en {symbol}")
        return
    qty = float(p["qty"])
    sell_qty = qty * max(0.01, min(1.0, pct/100.0))
    sell_qty = math.floor(sell_qty * 1e8) / 1e8
    sell = paper_sell(symbol, sell_qty)
    if sell.ok:
        qty_left = qty - sell_qty
        if qty_left <= 1e-10:
            bot.db.delete_position(symbol)
        else:
            bot.db.update_position_fields(symbol, {"qty": qty_left, "fee_sell": float(p["fee_sell"]) + float(sell.fee), "ts": now_ms()})
        await update.message.reply_text(f"✅ PAPER SELL {symbol} qty={sell_qty:.8f} @ {sell.price:.6f} fee≈{sell.fee:.4f}")
    else:
        await update.message.reply_text(f"❌ SELL falló: {sell.err}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "🧠 BOT2 PAPER comandos:\n"
        "/status\n"
        "/mode CONSERVADOR|NORMAL|AGRESIVO\n"
        "/buy SYMBOL USD\n"
        "/sell SYMBOL [PCT]\n\n"
        "Amarre con BOT1 por Telegram:\n"
        "En el chat de BOT2 manda:\n"
        "SIG ADAUSDT 20 0.403 2:50,3.5:30,5:20 2.0"
    )

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # señales por texto desde BOT1 (telegram)
    try:
        if not update.message:
            return
        text = update.message.text or ""
        sig = parse_sig_message(text)
        if not sig:
            return
        ok, msg = bot.paper_open_from_signal(sig)
        await update.message.reply_text(msg if ok else ("❌ " + msg))
    except Exception:
        await update.message.reply_text("⚠️ error parsing SIG:\n" + traceback.format_exc())

# =========================
# Loop background
# =========================
async def bg_loop(app: Application):
    while True:
        try:
            # 1) Si la fuente es sqlite, trae señales NEW y ejecútalas
            if SIGNAL_SOURCE == "sqlite":
                sigs = bot.fetch_signals_from_sqlite()
                for s in sigs:
                    ok, msg = bot.paper_open_from_signal(s)
                    bot.mark_signal_sqlite(s["id"], "EXECUTED" if ok else "REJECTED", msg)
                    # opcional: avisa al chat
                    if TELEGRAM_CHAT_ID_AUTO:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID_AUTO, text=msg if ok else ("❌ " + msg))

            # 2) Gestiona TP/SL de posiciones existentes
            notes = bot.manage_positions_tp_sl()
            if notes and TELEGRAM_CHAT_ID_AUTO:
                for n in notes[:6]:
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID_AUTO, text=n)

        except Exception:
            if TELEGRAM_CHAT_ID_AUTO:
                try:
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID_AUTO, text="⚠️ bg_loop error:\n" + traceback.format_exc()[:3500])
                except Exception:
                    pass

        await app.bot.sleep(LOOP_SEC)

def main():
    if not TELEGRAM_TOKEN_AUTO:
        raise SystemExit("Falta TELEGRAM_TOKEN_AUTO en env")

    app = Application.builder().token(TELEGRAM_TOKEN_AUTO).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async def post_init(application: Application):
        # mensaje de arranque
        if TELEGRAM_CHAT_ID_AUTO:
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID_AUTO,
                text=f"✅ BOT2 PAPER ONLINE | mode={MODE} | source={SIGNAL_SOURCE} | {now_utc()}"
            )
        application.create_task(bg_loop(application))

    app.post_init = post_init
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
