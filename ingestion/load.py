"""Idempotent loading: COPY into a staging table, then INSERT ... ON CONFLICT DO UPDATE."""

from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd
import psycopg
from psycopg import sql

from core.db import connect


def upsert(
    conn: psycopg.Connection,
    table: str,
    df: pd.DataFrame,
    key_cols: list[str],
    value_cols: list[str],
) -> int:
    """Insert new rows and update rows whose values changed. Returns rows inserted + updated.

    Unchanged rows are skipped by the WHERE clause, so re-running a load reports 0 and
    `updated_at` only moves when the source actually revised a value.
    """
    if df.empty:
        return 0
    cols = key_cols + value_cols
    stage = sql.Identifier(f"_stage_{table}")
    target = sql.Identifier(table)
    col_list = sql.SQL(", ").join(map(sql.Identifier, cols))

    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(stage))
        cur.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP").format(
                stage, target
            )
        )
        with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(stage, col_list)) as copy:
            # astype(object) turns numpy scalars into plain Python values psycopg can adapt
            for row in df[cols].astype(object).itertuples(index=False, name=None):
                copy.write_row(row)

        cur.execute(
            sql.SQL(
                """
                INSERT INTO {target} ({cols})
                SELECT {cols} FROM {stage}
                ON CONFLICT ({keys}) DO UPDATE
                SET {assignments}, updated_at = now()
                WHERE {changed}
                """
            ).format(
                target=target,
                cols=col_list,
                stage=stage,
                keys=sql.SQL(", ").join(map(sql.Identifier, key_cols)),
                assignments=sql.SQL(", ").join(
                    sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in value_cols
                ),
                changed=sql.SQL(" OR ").join(
                    sql.SQL("{t}.{c} IS DISTINCT FROM EXCLUDED.{c}").format(
                        t=target, c=sql.Identifier(c)
                    )
                    for c in value_cols
                ),
            )
        )
        return cur.rowcount


class RunStats:
    rows_fetched: int = 0
    rows_upserted: int = 0


@contextmanager
def track_run(job: str) -> Iterator[RunStats]:
    """Record a job in etl_runs. Uses its own connection so failures are logged
    even when the job's data transaction rolls back."""
    stats = RunStats()
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO etl_runs (job) VALUES (%s) RETURNING run_id", (job,)
        ).fetchone()[0]
    try:
        yield stats
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE etl_runs SET status = 'failed', finished_at = now(), error = %s "
                "WHERE run_id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id),
            )
        raise
    with connect() as conn:
        conn.execute(
            "UPDATE etl_runs SET status = 'success', finished_at = now(), "
            "rows_fetched = %s, rows_upserted = %s WHERE run_id = %s",
            (stats.rows_fetched, stats.rows_upserted, run_id),
        )
