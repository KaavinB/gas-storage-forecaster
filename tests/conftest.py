import psycopg
import pytest

from core.config import database_url


@pytest.fixture
def db_conn():
    """A connection whose work is always rolled back, so tests never touch real data."""
    try:
        conn = psycopg.connect(database_url(), connect_timeout=3)
    except (psycopg.OperationalError, RuntimeError) as exc:
        pytest.skip(f"Postgres not available ({exc}); run `make up`")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
