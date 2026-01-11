# db_sqlite.py
import os
import json
import time
import sqlite3
import threading
from typing import Any, Optional, Dict, List

DEFAULT_DB_PATH = os.getenv("SQLITE_DB_PATH", "bot_state.sqlite3")


class BotDB:
    """
    SQLite robusto:
    - WAL (mejor concurrencia)
    - busy_timeout (evita 'database is locked')
    - lock Python para operaciones críticas
    """

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        with self._lock:
            cur = self._conn.cursor()

            # PRAGMAs de estabilidad
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA busy_timeout=5000;")  # 5s

            # =========================
            # positions (TU ESQUEMA REAL)
            # =========================
            cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                symbol      TEXT PRIMARY KEY,
                in_position INTEGER NOT NULL DEFAULT 1,
                usd         REAL    NOT NULL,
                entry       REAL    NOT NULL,
                qty         REAL    NOT NULL,
                buy_cost    REAL    NOT NULL,
                fee_buy     REAL    NOT NULL,
                fee_sell    REAL    NOT NULL,
                sl_pct      REAL    NOT NULL,
                ladder_json TEXT    NOT NULL,
                hits_json   TEXT    NOT NULL,
                be_sent     INTEGER NOT NULL DEFAULT 0,
                ts          INTEGER NOT NULL
            );
            """)

            # =========================
            # kv_state (cooldowns, last_alert, etc.)
            # =========================
            cur.execute("""
            CREATE TABLE IF NOT EXISTS kv_state (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)

            # =========================
            # kv_runtime (ACTIVE_MODE, etc.)
            # =========================
            cur.execute("""
            CREATE TABLE IF NOT EXISTS kv_runtime (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)

            self._conn.commit()

    # ---------- helpers ----------
    def close(self):
        with self._lock:
            self._conn.close()

    def _now(self) -> int:
        return int(time.time())

    def _jdump(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _jload(self, value: Optional[str], default: Any):
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default

    # ---------- kv ----------
    def _set_kv(self, table: str, key: str, value: Any):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"INSERT INTO {table} (k, v, updated_at) VALUES (?, ?, ?) "
                f"ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at;",
                (key, self._jdump(value), self._now())
            )
            self._conn.commit()

    def _get_kv(self, table: str, key: str, default: Any = None) -> Any:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"SELECT v FROM {table} WHERE k=?;", (key,))
            row = cur.fetchone()
            if not row:
                return default
            return self._jload(row["v"], default)

    def state_set(self, key: str, value: Any):
        self._set_kv("kv_state", key, value)

    def state_get(self, key: str, default: Any = None) -> Any:
        return self._get_kv("kv_state", key, default)

    def runtime_set(self, key: str, value: Any):
        self._set_kv("kv_runtime", key, value)

    def runtime_get(self, key: str, default: Any = None) -> Any:
        return self._get_kv("kv_runtime", key, default)

    # ---------- positions ----------
    def upsert_position(self, symbol: str, data: Dict[str, Any]):
        """
        Guarda/actualiza según TU tabla real:

        columns:
          symbol, in_position, usd, entry, qty, buy_cost, fee_buy, fee_sell,
          sl_pct, ladder_json, hits_json, be_sent, ts

        data aceptado (recomendado):
          usd, entry, qty, buy_cost, fee_buy, fee_sell, sl_pct,
          ladder (dict/list/str), hits (dict/list/str),
          in_position, be_sent, ts
        """
        with self._lock:
            cur = self._conn.cursor()

            in_position = int(data.get("in_position", 1))
            usd = float(data.get("usd", 0) or 0)
            entry = float(data.get("entry", 0) or 0)
            qty = float(data.get("qty", 0) or 0)

            # buy_cost: si no viene, usa usd (costo total aprox)
            buy_cost = float(data.get("buy_cost", usd) or 0)

            fee_buy = float(data.get("fee_buy", 0) or 0)
            fee_sell = float(data.get("fee_sell", 0) or 0)
            sl_pct = float(data.get("sl_pct", 0) or 0)
            be_sent = int(data.get("be_sent", 0))
            ts = int(data.get("ts", self._now()))

            # ladder_json / hits_json deben ser TEXT NOT NULL
            ladder = data.get("ladder", data.get("ladder_json", ""))  # acepta ambos
            hits = data.get("hits", data.get("hits_json", ""))

            if isinstance(ladder, (dict, list)):
                ladder_json = self._jdump(ladder)
            elif ladder is None:
                ladder_json = ""
            else:
                ladder_json = str(ladder)

            if isinstance(hits, (dict, list)):
                hits_json = self._jdump(hits)
            elif hits is None:
                hits_json = ""
            else:
                hits_json = str(hits)

            cur.execute("""
            INSERT INTO positions (
                symbol, in_position, usd, entry, qty, buy_cost, fee_buy, fee_sell,
                sl_pct, ladder_json, hits_json, be_sent, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                in_position=excluded.in_position,
                usd=excluded.usd,
                entry=excluded.entry,
                qty=excluded.qty,
                buy_cost=excluded.buy_cost,
                fee_buy=excluded.fee_buy,
                fee_sell=excluded.fee_sell,
                sl_pct=excluded.sl_pct,
                ladder_json=excluded.ladder_json,
                hits_json=excluded.hits_json,
                be_sent=excluded.be_sent,
                ts=excluded.ts;
            """, (
                symbol, in_position, usd, entry, qty, buy_cost, fee_buy, fee_sell,
                sl_pct, ladder_json, hits_json, be_sent, ts
            ))

            self._conn.commit()

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM positions WHERE symbol=?;", (symbol,))
            row = cur.fetchone()
            if not row:
                return None

            # Devolvemos todo + ladder/hits parseados como conveniencia
            d = dict(row)
            d["ladder"] = self._jload(d.get("ladder_json"), None)
            d["hits"] = self._jload(d.get("hits_json"), None)
            return d

    def list_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM positions ORDER BY ts DESC;")
            rows = cur.fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["ladder"] = self._jload(d.get("ladder_json"), None)
                d["hits"] = self._jload(d.get("hits_json"), None)
                out.append(d)
            return out

    def delete_position(self, symbol: str):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM positions WHERE symbol=?;", (symbol,))
            self._conn.commit()

    # Utilidad: marcar/actualizar fields rápidos
    def update_position_fields(self, symbol: str, **fields):
        """
        Ej: DB.update_position_fields("ADAUSDT", entry=0.403, usd=180, ts=int(time.time()))
        """
        if not fields:
            return
        allowed = {
            "in_position", "usd", "entry", "qty", "buy_cost", "fee_buy", "fee_sell",
            "sl_pct", "ladder_json", "hits_json", "be_sent", "ts"
        }
        set_parts = []
        params = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            set_parts.append(f"{k}=?")
            params.append(v)
        if not set_parts:
            return
        params.append(symbol)

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"UPDATE positions SET {', '.join(set_parts)} WHERE symbol=?;", params)
            self._conn.commit()
