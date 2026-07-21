"""Database access. Reads DATABASE_URL from the environment (Neon PostgreSQL).

The pool is created lazily so the app can start (and serve the login page)
even before the database is reachable.
"""
import os
import threading
from contextlib import contextmanager

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                import psycopg2.pool

                dsn = os.environ["DATABASE_URL"]
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, dsn)
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


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
