"""Thin Postgres helpers built on psycopg 3."""

from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd
import psycopg

from core.config import database_url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Connection that commits on success and rolls back on any exception."""
    with psycopg.connect(database_url()) as conn:
        yield conn


def read_sql(query: str, params: dict | None = None) -> pd.DataFrame:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)
