"""Database access. Reads DATABASE_URL from the environment (Neon PostgreSQL).

The pool is created lazily so the app can start (and serve the login page)
even before the database is reachable. Checkouts are gated by a semaphore:
psycopg2's pool raises "pool exhausted" instead of queueing, so without the
gate a burst of concurrent requests (the dashboard fires six on page load)
errors out rather than waiting.
"""
import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

POOL_MAX = 8

_pool = None
_pool_lock = threading.Lock()
_checkout_gate = threading.BoundedSemaphore(POOL_MAX)


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = os.environ["DATABASE_URL"]
                _pool = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX, dsn)
    return _pool


def _checkout(pool):
    """Get a connection, discarding any that died while idle (Neon closes
    idle connections; the pool would happily hand them back)."""
    for _ in range(POOL_MAX + 1):
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return conn
        except psycopg2.Error:
            pool.putconn(conn, close=True)
    raise RuntimeError("could not obtain a healthy database connection")


@contextmanager
def get_conn():
    pool = _get_pool()
    with _checkout_gate:  # blocks (rather than errors) when all conns are busy
        conn = _checkout(pool)
        broken = False
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except psycopg2.Error:
                broken = True
            raise
        finally:
            pool.putconn(conn, close=broken)


def query_all(sql, params=None):
    """Run a SELECT and return a list of dicts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_one(sql, params=None):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Run a write statement; returns affected row count."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount
