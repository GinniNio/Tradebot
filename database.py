"""
database.py — SQLite schema and helper functions.

Tables:
  smart_wallets   — wallets we're tracking (address, win_rate, trade_count, etc.)
  wallet_activity — recent buys/sells observed per wallet
  signals         — triggered buy/sell signals (wallet or zombie)
  open_positions  — currently held tokens
  trade_history   — completed trades with P&L
  zombie_watchlist— tokens being monitored for revival
"""

import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager
from config import DB_PATH

logger = logging.getLogger(__name__)


# ─── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_wallets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    address       TEXT    UNIQUE NOT NULL,
    chain         TEXT    NOT NULL DEFAULT 'solana',
    win_rate      REAL    DEFAULT 0.0,
    trade_count   INTEGER DEFAULT 0,
    profit_usd    REAL    DEFAULT 0.0,
    added_at      TEXT    NOT NULL,
    last_seen     TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS wallet_activity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet        TEXT    NOT NULL,
    chain         TEXT    NOT NULL,
    token_address TEXT    NOT NULL,
    token_symbol  TEXT,
    action        TEXT    NOT NULL CHECK(action IN ('buy', 'sell')),
    amount_usd    REAL,
    tx_hash       TEXT    UNIQUE,
    observed_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wa_token_time
    ON wallet_activity(token_address, observed_at);
CREATE INDEX IF NOT EXISTS idx_wa_wallet
    ON wallet_activity(wallet);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_type   TEXT    NOT NULL CHECK(signal_type IN ('wallet', 'zombie', 'launch')),
    chain         TEXT    NOT NULL,
    token_address TEXT    NOT NULL,
    token_symbol  TEXT,
    trigger_count INTEGER DEFAULT 1,   -- for wallet: how many wallets triggered it
    extra_data    TEXT,                -- JSON blob with additional context
    created_at    TEXT    NOT NULL,
    acted         INTEGER DEFAULT 0    -- 1 = trade was placed
);

CREATE TABLE IF NOT EXISTS open_positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chain             TEXT    NOT NULL,
    token_address     TEXT    NOT NULL,
    token_symbol      TEXT,
    entry_price_usd   REAL    NOT NULL,
    entry_amount_sol  REAL,
    entry_token_amount INTEGER,                   -- raw token base units received from buy
    size_usd          REAL,
    stop_loss_usd     REAL,
    take_profit_usd   REAL,
    signal_id         INTEGER REFERENCES signals(id),
    signal_type       TEXT,                       -- 'wallet' or 'zombie'
    opened_at         TEXT    NOT NULL,
    tx_hash           TEXT
);

CREATE TABLE IF NOT EXISTS trade_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chain             TEXT    NOT NULL,
    token_address     TEXT    NOT NULL,
    token_symbol      TEXT,
    entry_price_usd   REAL,
    exit_price_usd    REAL,
    entry_amount_sol  REAL,
    entry_token_amount INTEGER,
    pnl_usd           REAL,
    pnl_pct           REAL,
    exit_reason       TEXT,   -- 'stop_loss', 'take_profit', 'manual', 'zombie_fade'
    signal_type       TEXT,
    opened_at         TEXT,
    closed_at         TEXT,
    entry_tx          TEXT,
    exit_tx           TEXT
);

CREATE TABLE IF NOT EXISTS zombie_watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain           TEXT    NOT NULL,
    token_address   TEXT    NOT NULL UNIQUE,
    token_symbol    TEXT,
    name            TEXT,
    peak_volume_usd REAL,
    last_active_at  TEXT,   -- last time it had meaningful volume
    dormant_since   TEXT,
    added_at        TEXT    NOT NULL,
    status          TEXT    DEFAULT 'watching'  -- 'watching', 'triggered', 'ignored'
);
CREATE INDEX IF NOT EXISTS idx_zombie_status
    ON zombie_watchlist(status);

-- ─── Phase 3: signal outcome measurement layer ────────────────────────────────
-- Every fired signal (regardless of strategy state) gets one row here. price_at_signal
-- is captured synchronously from already-fetched Dex stats. price_5m/15m/1h/24h are
-- filled in later by the background worker that drains pending_price_checks.
-- source='live' for real-time signals; 'backfill' for historical signals processed
-- by the Phase 4 backfill script (kept separate so live averages aren't contaminated).
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id                   INTEGER NOT NULL,
    strategy                    TEXT    NOT NULL,
    token_address               TEXT    NOT NULL,
    chain                       TEXT    NOT NULL,
    price_at_signal             REAL,
    price_5m                    REAL,
    price_15m                   REAL,
    price_1h                    REAL,
    price_24h                   REAL,
    price_3d                    REAL,
    price_7d                    REAL,
    route_available_at_signal   INTEGER,
    source                      TEXT    NOT NULL DEFAULT 'live',
    -- Default applies only on fresh installs; existing rows are set by code via _now().
    created_at                  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(signal_id, source)
);
CREATE INDEX IF NOT EXISTS idx_so_strategy_source ON signal_outcomes(strategy, source);
CREATE INDEX IF NOT EXISTS idx_so_signal ON signal_outcomes(signal_id);

-- DB-backed scheduler. Each row is a snapshot the worker should fulfill once
-- due_at <= now(). Survives bot restarts — pure asyncio tasks would lose the
-- 24h checks every time the bot reboots (which is frequently).
CREATE TABLE IF NOT EXISTS pending_price_checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_outcome_id INTEGER NOT NULL REFERENCES signal_outcomes(id),
    token_address     TEXT    NOT NULL,
    chain             TEXT    NOT NULL,
    check_type        TEXT    NOT NULL,   -- 'price_5m', 'price_15m', 'price_1h', 'price_24h'
    due_at            TEXT    NOT NULL,
    completed_at      TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending',  -- 'pending', 'done', 'failed'
    error             TEXT,
    -- Default applies only on fresh installs; existing rows are set by code via _now().
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ppc_due_status ON pending_price_checks(status, due_at);
"""


# ─── Connection helper ─────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist. Apply idempotent column migrations."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
    logger.info("Database initialised at %s", DB_PATH)


def _apply_migrations(conn):
    """Add new columns to existing tables. Safe to run on every startup."""
    migrations = [
        ("open_positions",  "entry_token_amount", "INTEGER"),
        ("open_positions",  "signal_type",        "TEXT"),
        ("trade_history",   "entry_token_amount", "INTEGER"),
        ("signal_outcomes", "price_3d",           "REAL"),
        ("signal_outcomes", "price_7d",           "REAL"),
        # ─── Phase 1 of options-portfolio rebuild ──────────────────────────
        # strategy_tag distinguishes v1-momentum-baseline (legacy) from v2-options
        # (the rebuild) and any future variants. SQLite applies the DEFAULT to
        # existing rows on ADD COLUMN, so legacy data backfills as v1.
        ("signals",         "strategy_tag",       "TEXT DEFAULT 'v1-momentum-baseline'"),
        ("open_positions",  "strategy_tag",       "TEXT DEFAULT 'v1-momentum-baseline'"),
        ("trade_history",   "strategy_tag",       "TEXT DEFAULT 'v1-momentum-baseline'"),
        # tp_tier_state holds a JSON list of which TP tiers have already been
        # partially exited (e.g. '[100]' after +100% tier fired). NULL for v1.
        ("open_positions",  "tp_tier_state",      "TEXT"),
        # peak_price_usd tracks the highest price seen since position open;
        # used by v2's trailing-stop logic on the runner portion. NULL for v1.
        ("open_positions",  "peak_price_usd",     "REAL"),
        # remaining_token_amount: for v2 tiered exits, decrements as tiers fire.
        # NULL for v1 (which exits the full position in one go).
        ("open_positions",  "remaining_token_amount", "INTEGER"),
    ]
    for table, column, coltype in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            logger.info("Migration: added %s.%s", table, column)
        except sqlite3.OperationalError as e:
            # Column already exists; ignore
            if "duplicate column" not in str(e).lower():
                raise

    _migrate_signals_check_constraint(conn)


def _migrate_signals_check_constraint(conn):
    """
    Expand signals.signal_type CHECK constraint to include research feeds.

    SQLite does not support ALTER TABLE ... MODIFY CONSTRAINT, so this
    recreates the table. The migration is idempotent: it reads the current
    schema and returns immediately if 'launch' is already present.

    Foreign-key enforcement is disabled for the session during the swap
    (PRAGMA foreign_keys=OFF) because open_positions.signal_id references
    signals.id. The FK is re-enabled before returning. SQLite only enforces
    FKs on INSERT/UPDATE, not on DROP/RENAME, so existing data is unaffected.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()
    if not row:
        return  # signals not yet created; SCHEMA will create it with the right CHECK
    if "'launch'" in row[0] and "'swing'" in row[0]:
        return  # already migrated

    logger.info("Migration: expanding signals.signal_type CHECK to include 'launch'...")
    # executescript issues an implicit COMMIT before running — safe here because
    # _apply_migrations is called from init_db() which commits after returning.
    conn.executescript("""
        PRAGMA foreign_keys=OFF;
        CREATE TABLE IF NOT EXISTS signals_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type   TEXT    NOT NULL CHECK(signal_type IN ('wallet', 'zombie', 'launch', 'swing')),
            chain         TEXT    NOT NULL,
            token_address TEXT    NOT NULL,
            token_symbol  TEXT,
            trigger_count INTEGER DEFAULT 1,
            extra_data    TEXT,
            created_at    TEXT    NOT NULL,
            acted         INTEGER DEFAULT 0,
            strategy_tag  TEXT DEFAULT 'v1-momentum-baseline'
        );
        INSERT OR IGNORE INTO signals_new SELECT * FROM signals;
        DROP TABLE signals;
        ALTER TABLE signals_new RENAME TO signals;
        PRAGMA foreign_keys=ON;
    """)
    logger.info("Migration: signals.signal_type CHECK now includes 'launch'")


# ─── Smart Wallet helpers ──────────────────────────────────────────────────────

def add_smart_wallet(address: str, chain: str = "solana", win_rate: float = 0.0,
                     trade_count: int = 0, notes: str = "") -> bool:
    """Add a wallet to tracking. Returns True if inserted, False if already exists."""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO smart_wallets
                   (address, chain, win_rate, trade_count, added_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (address, chain, win_rate, trade_count, _now(), notes)
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error("add_smart_wallet error: %s", e)
        return False


def get_smart_wallets(chain: str = None) -> list[dict]:
    with get_conn() as conn:
        if chain:
            rows = conn.execute(
                "SELECT * FROM smart_wallets WHERE chain = ?", (chain,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM smart_wallets").fetchall()
        return [dict(r) for r in rows]


def update_wallet_last_seen(address: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE smart_wallets SET last_seen = ? WHERE address = ?",
            (_now(), address)
        )


# ─── Wallet Activity helpers ───────────────────────────────────────────────────

def record_wallet_activity(wallet: str, chain: str, token_address: str,
                            token_symbol: str, action: str, amount_usd: float,
                            tx_hash: str = None) -> bool:
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO wallet_activity
                   (wallet, chain, token_address, token_symbol, action, amount_usd, tx_hash, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (wallet, chain, token_address, token_symbol, action, amount_usd, tx_hash, _now())
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error("record_wallet_activity error: %s", e)
        return False


def count_wallet_buys(token_address: str, within_minutes: int = 10) -> list[dict]:
    """Return list of tracked wallets that bought a token within the time window."""
    cutoff = _minutes_ago(within_minutes)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT wa.wallet, wa.amount_usd, wa.observed_at
               FROM wallet_activity wa
               INNER JOIN smart_wallets sw ON sw.address = wa.wallet
               WHERE wa.token_address = ?
                 AND wa.action = 'buy'
                 AND wa.observed_at >= ?""",
            (token_address, cutoff)
        ).fetchall()
        return [dict(r) for r in rows]


def get_hot_tokens(within_minutes: int = 10, min_wallet_count: int = 3) -> list[dict]:
    """Return tokens that have been bought by multiple tracked wallets recently."""
    cutoff = _minutes_ago(within_minutes)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT wa.token_address, wa.token_symbol, wa.chain,
                      COUNT(DISTINCT wa.wallet) as wallet_count
               FROM wallet_activity wa
               INNER JOIN smart_wallets sw ON sw.address = wa.wallet
               WHERE wa.action = 'buy'
                 AND wa.observed_at >= ?
               GROUP BY wa.token_address
               HAVING wallet_count >= ?
               ORDER BY wallet_count DESC""",
            (cutoff, min_wallet_count)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Signal helpers ────────────────────────────────────────────────────────────

def create_signal(signal_type: str, chain: str, token_address: str,
                  token_symbol: str, trigger_count: int = 1,
                  extra_data: str = None,
                  strategy_tag: str = "v1-momentum-baseline") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (signal_type, chain, token_address, token_symbol, trigger_count,
                extra_data, created_at, strategy_tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_type, chain, token_address, token_symbol, trigger_count,
             extra_data, _now(), strategy_tag)
        )
        return cur.lastrowid


def mark_signal_acted(signal_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE signals SET acted = 1 WHERE id = ?", (signal_id,))


def get_recent_launch_signal(token_address: str, within_hours: int = 2) -> bool:
    """Return True if a 'launch' signal for this token already exists within the window.
    Used by graduation_tracker to prevent re-firing on the same graduate."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=within_hours)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id FROM signals
               WHERE token_address = ? AND signal_type = 'launch' AND created_at >= ?
               LIMIT 1""",
            (token_address, cutoff)
        ).fetchone()
        return row is not None


def get_recent_signal(token_address: str, signal_type: str, within_hours: int) -> bool:
    """Whether this feed recently surfaced a token."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=within_hours)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id FROM signals WHERE token_address = ? AND signal_type = ?
               AND created_at >= ? LIMIT 1""",
            (token_address, signal_type, cutoff),
        ).fetchone()
        return row is not None


def get_pending_signals() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE acted = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Position helpers ──────────────────────────────────────────────────────────

def open_position(chain: str, token_address: str, token_symbol: str,
                  entry_price_usd: float, entry_amount_sol: float,
                  size_usd: float, stop_loss_usd: float, take_profit_usd: float,
                  entry_token_amount: int = 0,
                  signal_id: int = None, signal_type: str = None,
                  tx_hash: str = None,
                  strategy_tag: str = "v1-momentum-baseline",
                  tp_tier_state: str = None,
                  peak_price_usd: float = None,
                  remaining_token_amount: int = None) -> int:
    """
    Record a new open position. `entry_token_amount` is the raw base-unit quantity
    of the bought token (from Jupiter quote.outAmount) — required for live sells.

    strategy_tag determines the trade-management profile (v1-momentum-baseline:
    single SL/TP; v2-options: tiered TPs with no stop, trailing on the runner).
    For v2 positions, peak_price_usd should be initialized to entry_price_usd and
    remaining_token_amount to entry_token_amount; tp_tier_state starts as '[]'.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO open_positions
               (chain, token_address, token_symbol, entry_price_usd, entry_amount_sol,
                entry_token_amount, size_usd, stop_loss_usd, take_profit_usd,
                signal_id, signal_type, opened_at, tx_hash,
                strategy_tag, tp_tier_state, peak_price_usd, remaining_token_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chain, token_address, token_symbol, entry_price_usd, entry_amount_sol,
             entry_token_amount, size_usd, stop_loss_usd, take_profit_usd,
             signal_id, signal_type, _now(), tx_hash,
             strategy_tag, tp_tier_state, peak_price_usd, remaining_token_amount)
        )
        return cur.lastrowid


def get_open_positions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM open_positions ORDER BY opened_at").fetchall()
        return [dict(r) for r in rows]


def close_position(position_id: int, exit_price_usd: float, exit_reason: str,
                   exit_tx: str = None):
    """
    Move a position from open_positions → trade_history.
    Should only be called AFTER the sell tx has actually been sent (or simulated in paper mode).
    """
    with get_conn() as conn:
        pos = conn.execute(
            "SELECT * FROM open_positions WHERE id = ?", (position_id,)
        ).fetchone()
        if not pos:
            logger.warning("close_position: id %d not found", position_id)
            return
        pos = dict(pos)
        entry_px = pos["entry_price_usd"] or 0
        size_usd = pos.get("size_usd") or 0
        if entry_px > 0:
            pnl_usd = (exit_price_usd - entry_px) * (size_usd / entry_px)
            pnl_pct = ((exit_price_usd - entry_px) / entry_px) * 100
        else:
            pnl_usd, pnl_pct = 0.0, 0.0

        conn.execute(
            """INSERT INTO trade_history
               (chain, token_address, token_symbol, entry_price_usd, exit_price_usd,
                entry_amount_sol, entry_token_amount, pnl_usd, pnl_pct, exit_reason,
                signal_type, opened_at, closed_at, entry_tx, exit_tx, strategy_tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["chain"], pos["token_address"], pos["token_symbol"],
             entry_px, exit_price_usd, pos.get("entry_amount_sol"),
             pos.get("entry_token_amount"), pnl_usd, pnl_pct, exit_reason,
             pos.get("signal_type"), pos["opened_at"], _now(),
             pos.get("tx_hash"), exit_tx,
             pos.get("strategy_tag") or "v1-momentum-baseline")
        )
        conn.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        logger.info("Closed position %d | %s | %.2f%% PnL", position_id,
                    pos["token_symbol"], pnl_pct)


# ─── Zombie helpers ────────────────────────────────────────────────────────────

def add_to_zombie_watchlist(chain: str, token_address: str, token_symbol: str,
                             name: str = "", peak_volume_usd: float = 0.0,
                             last_active_at: str = None) -> bool:
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO zombie_watchlist
                   (chain, token_address, token_symbol, name, peak_volume_usd,
                    last_active_at, dormant_since, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (chain, token_address, token_symbol, name, peak_volume_usd,
                 last_active_at, last_active_at, _now())
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error("add_to_zombie_watchlist error: %s", e)
        return False


def get_zombie_watchlist(status: str = "watching") -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM zombie_watchlist WHERE status = ?", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_zombie_status(token_address: str, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE zombie_watchlist SET status = ? WHERE token_address = ?",
            (status, token_address)
        )


# ─── Signal outcome helpers (Phase 3) ──────────────────────────────────────────

VALID_CHECK_TYPES = ("price_5m", "price_15m", "price_1h", "price_24h", "price_3d", "price_7d")


def insert_signal_outcome(signal_id: int, strategy: str, token_address: str,
                           chain: str, price_at_signal: float,
                           route_available_at_signal: int | None,
                           source: str = "live") -> int:
    """
    Insert (or fetch existing) outcome row for this (signal_id, source) pair.
    Returns the row id. The UNIQUE constraint on (signal_id, source) means
    multiple calls for the same signal+source are idempotent.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO signal_outcomes
               (signal_id, strategy, token_address, chain, price_at_signal,
                route_available_at_signal, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, strategy, token_address, chain, price_at_signal,
             route_available_at_signal, source, _now())
        )
        if cur.rowcount > 0:
            return cur.lastrowid
        existing = conn.execute(
            "SELECT id FROM signal_outcomes WHERE signal_id = ? AND source = ?",
            (signal_id, source)
        ).fetchone()
        return existing["id"] if existing else 0


def schedule_price_check(signal_outcome_id: int, token_address: str, chain: str,
                          check_type: str, due_at: str) -> int:
    """Enqueue a price snapshot to be fulfilled by the background worker."""
    if check_type not in VALID_CHECK_TYPES:
        raise ValueError(f"invalid check_type: {check_type}")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pending_price_checks
               (signal_outcome_id, token_address, chain, check_type, due_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (signal_outcome_id, token_address, chain, check_type, due_at, _now())
        )
        return cur.lastrowid


def get_due_price_checks(limit: int = 200) -> list[dict]:
    """Pending checks where due_at <= now. Caller batches by chain."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM pending_price_checks
               WHERE status = 'pending' AND due_at <= ?
               ORDER BY due_at
               LIMIT ?""",
            (_now(), limit)
        ).fetchall()
        return [dict(r) for r in rows]


def complete_price_check(check_id: int, price: float, signal_outcome_id: int,
                          check_type: str):
    """Atomically: write price into the outcome row + mark the check done."""
    if check_type not in VALID_CHECK_TYPES:
        raise ValueError(f"invalid check_type: {check_type}")
    with get_conn() as conn:
        # check_type is whitelisted above, so interpolation here is safe.
        conn.execute(
            f"UPDATE signal_outcomes SET {check_type} = ? WHERE id = ?",
            (price, signal_outcome_id)
        )
        conn.execute(
            """UPDATE pending_price_checks
               SET status = 'done', completed_at = ?
               WHERE id = ?""",
            (_now(), check_id)
        )


def fail_price_check(check_id: int, error: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE pending_price_checks
               SET status = 'failed', completed_at = ?, error = ?
               WHERE id = ?""",
            (_now(), (error or "")[:200], check_id)
        )


def get_signal_outcomes_list(source: "str | None" = None, limit: int = 50) -> list[dict]:
    """
    Return rows from signal_outcomes ordered by newest first.
    source: 'live', 'backfill', or None (no filter).
    """
    q = """SELECT id, signal_id, strategy, token_address, chain,
                  price_at_signal, price_5m, price_15m, price_1h, price_24h,
                  price_3d, price_7d,
                  route_available_at_signal, source, created_at
           FROM signal_outcomes"""
    params: tuple = ()
    if source in ("live", "backfill"):
        q += " WHERE source = ?"
        params = (source,)
    q += " ORDER BY id DESC LIMIT ?"
    params = params + (int(limit),)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def get_pending_price_checks_list(limit: int = 100,
                                   only_pending: bool = True,
                                   overdue_minutes: "int | None" = None) -> list[dict]:
    """
    Return rows from pending_price_checks ordered by due_at ascending.
    only_pending: if True (default), only status='pending' rows.
    overdue_minutes: if set, only rows where due_at < now - N minutes.
    """
    clauses = []
    params: list = []
    if only_pending:
        clauses.append("status = 'pending'")
    if overdue_minutes is not None:
        clauses.append("due_at < datetime('now', ?)")
        params.append(f"-{int(overdue_minutes)} minutes")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    q = f"""SELECT id, signal_outcome_id, token_address, chain, check_type,
                   due_at, completed_at, status, error, created_at
            FROM pending_price_checks{where}
            ORDER BY due_at ASC
            LIMIT ?"""
    params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def get_ops_health() -> dict:
    """
    Compact health snapshot for the dashboard / morning report.
    All counts are exact; timestamps are derived from the most recent
    DB activity as proxies (the bot doesn't write its own heartbeat row).
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM signal_outcomes WHERE source = 'live')
                AS live_outcomes,
              (SELECT COUNT(*) FROM signal_outcomes WHERE source = 'backfill')
                AS backfill_outcomes,
              (SELECT COUNT(*) FROM pending_price_checks WHERE status = 'pending')
                AS pending_checks,
              (SELECT COUNT(*) FROM pending_price_checks
                WHERE status = 'pending'
                  AND due_at < datetime('now', '-10 minutes'))
                AS overdue_checks,
              (SELECT COUNT(*) FROM open_positions) AS open_positions,
              (SELECT MAX(created_at) FROM signal_outcomes WHERE source = 'live')
                AS last_live_outcome_at,
              (SELECT MAX(completed_at) FROM pending_price_checks
                WHERE status = 'done')
                AS last_outcome_completed_at
        """).fetchone()
        return dict(row) if row else {}


def signal_outcomes_summary(source: str = "live") -> list[dict]:
    """
    Phase 5 verdict query. Returns one row per strategy with sample count,
    Jupiter route rate, and average post-signal returns at each horizon.
    NULL averages mean no checks have completed at that horizon yet.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT
                  strategy,
                  COUNT(*) as signals,
                  AVG(CASE WHEN route_available_at_signal IS NOT NULL
                       THEN CAST(route_available_at_signal AS REAL) END) as route_rate,
                  AVG(CASE WHEN price_5m IS NOT NULL AND price_at_signal > 0
                       THEN price_5m  / price_at_signal - 1.0 END) as avg_5m,
                  AVG(CASE WHEN price_15m IS NOT NULL AND price_at_signal > 0
                       THEN price_15m / price_at_signal - 1.0 END) as avg_15m,
                  AVG(CASE WHEN price_1h IS NOT NULL AND price_at_signal > 0
                       THEN price_1h  / price_at_signal - 1.0 END) as avg_1h,
                  AVG(CASE WHEN price_24h IS NOT NULL AND price_at_signal > 0
                       THEN price_24h / price_at_signal - 1.0 END) as avg_24h
                 ,AVG(CASE WHEN price_3d IS NOT NULL AND price_at_signal > 0
                       THEN price_3d / price_at_signal - 1.0 END) as avg_3d
                 ,AVG(CASE WHEN price_7d IS NOT NULL AND price_at_signal > 0
                       THEN price_7d / price_at_signal - 1.0 END) as avg_7d
               FROM signal_outcomes
               WHERE source = ?
                 AND price_at_signal IS NOT NULL
               GROUP BY strategy
               ORDER BY signals DESC""",
            (source,)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Utility ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()


def _minutes_ago(minutes: int) -> str:
    from datetime import timedelta
    return (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()


def get_trade_stats() -> dict:
    """Quick P&L summary for the terminal dashboard."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as total_trades,
                      SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(pnl_usd) as total_pnl_usd,
                      AVG(pnl_pct) as avg_pnl_pct
               FROM trade_history"""
        ).fetchone()
        return dict(row) if row else {}
