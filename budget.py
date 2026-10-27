"""Spending guard for paid model calls.

Every call reserves its worst-case cost in SQLite before the request is sent and settles from the returned usage
afterwards. If billing is uncertain (timeout, error, no usage), the reservation stays counted.

The record must survive restarts. If the database is missing or unreadable, paid calls are off; the server never
starts a fresh budget by itself. Create the record once with:  python budget.py init [--spent-before USD]
"""
import argparse
import datetime as dt
import os
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent

# USD per 1M tokens (input, output), from the Nebius Token Factory console on 2026-10-27.
PRICES = {
    'nvidia/Nemotron-3.5-Super-VL': (0.30, 0.90),
    'nvidia/Nemotron-3.5-Light': (0.06, 0.24),
    'nvidia/Nemotron-3-Ultra': (1.00, 3.00),
}
FALLBACK_PRICE = (1.00, 3.00)   # unknown model names are charged at the most expensive listed rate
# Upper bound on prompt tokens: byte-level BPE never makes more tokens than UTF-8 bytes; the chat template adds a
# few special tokens per message. Real English prompts use about a quarter of this.
TEMPLATE_TOKENS_PER_MESSAGE = 16
TEMPLATE_TOKENS_FIXED = 64

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE spend (
    id INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    day TEXT NOT NULL,
    pool TEXT NOT NULL,
    visitor TEXT NOT NULL,
    model TEXT NOT NULL,
    reserved REAL NOT NULL,
    actual REAL,
    status TEXT NOT NULL          -- reserved, settled or uncertain
);
CREATE INDEX spend_day ON spend (day, pool);
CREATE INDEX spend_visitor ON spend (visitor, day);
"""

_inflight: set = set()
_inflight_lock = threading.Lock()


class BudgetError(RuntimeError):
    pass


def _setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def limits() -> dict:
    return {
        'total': float(_setting('BUDGET_TOTAL_USD', '5')),
        'judge': float(_setting('BUDGET_JUDGE_USD', '1')),
        'end': dt.date.fromisoformat(_setting('BUDGET_END_DATE', '2026-12-15')),
        'calls': {'public': int(_setting('VISITOR_DAILY_CALLS', '25')), 'judge': int(_setting('JUDGE_DAILY_CALLS', '200'))},
    }


def db_path() -> Path:
    return Path(_setting('BUDGET_DB', str(ROOT / 'data' / 'budget.db')))


def today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def price(model: str) -> tuple[float, float]:
    return PRICES.get(model, FALLBACK_PRICE)


def max_cost(model: str, messages: list, max_tokens: int) -> float:
    tokens = sum(len(str(m.get('content', '')).encode('utf-8')) + TEMPLATE_TOKENS_PER_MESSAGE for m in messages)
    p_in, p_out = price(model)
    return (tokens + TEMPLATE_TOKENS_FIXED) * p_in / 1e6 + max_tokens * p_out / 1e6


def usage_cost(model: str, usage: dict) -> float:
    p_in, p_out = price(model)
    return usage.get('prompt_tokens', 0) * p_in / 1e6 + usage.get('completion_tokens', 0) * p_out / 1e6


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.is_file():
        raise BudgetError('The AI teammate is paused: its spending record is missing on this server.')
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        if conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone() is None:
            raise sqlite3.DatabaseError('no version')
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        raise BudgetError('The AI teammate is paused: its spending record cannot be read.')
    return conn


def _spent(conn, where: str = '1', args: tuple = ()) -> float:
    row = conn.execute(f"SELECT COALESCE(SUM(CASE status WHEN 'settled' THEN actual ELSE reserved END), 0) "
                       f'FROM spend WHERE {where}', args).fetchone()
    return row[0]


def _check(conn, pool: str, visitor: str, cost: float, day: dt.date):
    lim = limits()
    before = float(conn.execute("SELECT value FROM meta WHERE key = 'spent_before'").fetchone()[0])
    if before + _spent(conn) + cost > lim['total']:
        raise BudgetError('The AI teammate has used its whole budget for this demo. The simulation still works.')
    calls = conn.execute('SELECT COUNT(*) FROM spend WHERE visitor = ? AND day = ? AND pool = ?',
                         (visitor, day.isoformat(), pool)).fetchone()[0]
    if calls >= lim['calls'][pool]:
        raise BudgetError('You have used all AI messages for today. The simulation still works.')
    if pool == 'judge':
        if _spent(conn, "pool = 'judge'") + cost > lim['judge']:
            raise BudgetError('The judge budget for the AI teammate is used up. The simulation still works.')
        return
    public_total = lim['total'] - lim['judge'] - before
    earlier = _spent(conn, "pool = 'public' AND day < ?", (day.isoformat(),))
    days_left = max(1, (lim['end'] - day).days + 1)
    allowance = (public_total - earlier) / days_left
    if _spent(conn, "pool = 'public' AND day = ?", (day.isoformat(),)) + cost > allowance:
        raise BudgetError("The AI teammate has reached today's limit. It comes back tomorrow; the simulation still works.")


class Spend:
    """One visitor's access to one pool ('public' or 'judge'). Pass it to every paid model call."""

    def __init__(self, pool: str, visitor: str):
        if pool not in ('public', 'judge'):
            raise ValueError(pool)
        self.pool, self.visitor = pool, visitor

    def reserve(self, model: str, cost: float) -> int:
        with _inflight_lock:
            if self.visitor in _inflight:
                raise BudgetError('Please wait for the previous AI answer first.')
            _inflight.add(self.visitor)
        try:
            conn = _connect()
            try:
                day = today()
                conn.execute('BEGIN IMMEDIATE')
                try:
                    _check(conn, self.pool, self.visitor, cost, day)
                    cur = conn.execute('INSERT INTO spend (created, day, pool, visitor, model, reserved, status) '
                                       "VALUES (?, ?, ?, ?, ?, ?, 'reserved')",
                                       (time.time(), day.isoformat(), self.pool, self.visitor, model, cost))
                    conn.execute('COMMIT')
                except BaseException:
                    conn.execute('ROLLBACK')
                    raise
                return cur.lastrowid
            finally:
                conn.close()
        except BaseException:
            self._release()
            raise

    def finish(self, ticket: int, model: str, usage: dict | None):
        """Settle from real usage; with no usage the reservation stays counted as 'uncertain'."""
        try:
            conn = _connect()
            try:
                if usage and 'prompt_tokens' in usage and 'completion_tokens' in usage:
                    conn.execute("UPDATE spend SET actual = ?, status = 'settled' WHERE id = ?", (usage_cost(model, usage), ticket))
                else:
                    conn.execute("UPDATE spend SET status = 'uncertain' WHERE id = ?", (ticket,))
            finally:
                conn.close()
        except (BudgetError, sqlite3.Error):
            pass   # the row stays 'reserved', which is still counted at its worst-case cost
        finally:
            self._release()

    def _release(self):
        with _inflight_lock:
            _inflight.discard(self.visitor)


def report() -> dict:
    conn = _connect()
    try:
        before = float(conn.execute("SELECT value FROM meta WHERE key = 'spent_before'").fetchone()[0])
        day = today().isoformat()
        return {'spent_before': before, 'spent': round(_spent(conn), 4), 'today_public': round(_spent(conn, "pool = 'public' AND day = ?", (day,)), 4),
                'judge': round(_spent(conn, "pool = 'judge'"), 4),
                'calls': conn.execute('SELECT COUNT(*) FROM spend').fetchone()[0], 'limits': {k: str(v) for k, v in limits().items()}}
    finally:
        conn.close()


def init(spent_before: float):
    path = db_path()
    if path.exists():
        raise SystemExit(f'{path} already exists; refusing to overwrite a spending record.')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(SCHEMA)
        conn.executemany('INSERT INTO meta (key, value) VALUES (?, ?)',
                         [('version', '1'), ('spent_before', str(spent_before)), ('created', dt.datetime.now(dt.timezone.utc).isoformat())])
    conn.close()
    print(f'Created {path} (spent before: ${spent_before:.4f}).')


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')   # same settings as the server (BUDGET_DB and limits)
    parser = argparse.ArgumentParser(description='Incident Room spending record')
    sub = parser.add_subparsers(dest='command', required=True)
    p_init = sub.add_parser('init', help='create the spending record (once)')
    p_init.add_argument('--spent-before', type=float, default=0.0, help='USD already spent on development calls')
    sub.add_parser('report', help='show spending so far')
    args = parser.parse_args()
    if args.command == 'init':
        init(args.spent_before)
    else:
        print(report())
