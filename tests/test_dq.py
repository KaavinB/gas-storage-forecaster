from datetime import date

import pytest

from ingestion import dq


def _check(warn_above, fail_above):
    return dq.Check("c", "t", "SELECT 1", warn_above, fail_above, "")


@pytest.mark.parametrize(
    ("observed", "expected"),
    [(0, "pass"), (5, "pass"), (6, "warn"), (7, "warn"), (8, "fail"), (None, "fail")],
)
def test_evaluate_thresholds(observed, expected):
    assert dq.evaluate(observed, _check(warn_above=5, fail_above=7)) == expected


def test_every_check_query_runs_against_schema(db_conn):
    for check in dq.CHECKS:
        value = db_conn.execute(check.sql, {"as_of": date(2026, 1, 1)}).fetchone()
        assert value is not None, check.name


def test_freshness_checks_fail_when_data_is_stale(db_conn):
    """Evaluated as of a date far after any loaded data, every freshness check must fail."""
    for check in (c for c in dq.CHECKS if c.name.endswith("_freshness_days")):
        observed = db_conn.execute(check.sql, {"as_of": date(2035, 1, 1)}).fetchone()[0]
        assert dq.evaluate(observed, check) == "fail", check.name
