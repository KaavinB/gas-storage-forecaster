"""Evaluate alert rules, log every alert, and email the ones not yet delivered.

    python -m alerts.run                  # evaluate as of today, record, send
    python -m alerts.run --dry-run        # print what would fire; write and send nothing

Idempotency: alerts.alert_key is UNIQUE, so INSERT ... ON CONFLICT DO NOTHING records each
alert exactly once however often the job re-runs. Alerts whose email failed are retried on
the next run (for up to RETRY_DAYS) and never re-sent once delivered.
"""

import argparse
import json
from datetime import date

from psycopg.rows import dict_row

from alerts import notify
from alerts.rules import Alert, evaluate
from core.config import settings
from core.db import connect
from core.log import get_logger

log = get_logger("alerts")
RETRY_DAYS = 3


def record(conn, alerts: list[Alert], as_of: date) -> int:
    channel = settings()["alerts"]["channel"]
    inserted = 0
    for a in alerts:
        inserted += conn.execute(
            """INSERT INTO alerts (alert_key, rule_name, run_date, subject_date, severity,
                                   message, details, channel)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (alert_key) DO NOTHING""",
            (a.alert_key, a.rule_name, as_of, a.subject_date, a.severity, a.message,
             json.dumps(a.details), channel),
        ).rowcount
    return inserted


def deliver(conn) -> tuple[str, int]:
    with conn.cursor(row_factory=dict_row) as cur:
        pending = cur.execute(
            """SELECT alert_id, rule_name, severity, message FROM alerts
               WHERE delivery_status IN ('pending', 'failed')
                 AND created_at > now() - make_interval(days => %s)
               ORDER BY severity = 'critical' DESC, subject_date""",
            (RETRY_DAYS,),
        ).fetchall()
    if not pending:
        return "none", 0

    ids = [a["alert_id"] for a in pending]
    if not notify.email_configured():
        status = "skipped"
        log.warning("SMTP not configured; %d alert(s) logged but not emailed", len(ids))
    else:
        try:
            notify.send_email(notify.build_email(pending))
            status = "sent"
        except Exception:
            log.exception("email delivery failed; will retry next run")
            status = "failed"
    conn.execute(
        """UPDATE alerts SET delivery_status = %s,
                  sent_at = CASE WHEN %s = 'sent' THEN now() END
           WHERE alert_id = ANY(%s)""",
        (status, status, ids),
    )
    return status, len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate and deliver alerts")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-test", action="store_true",
                        help="send a clearly-labelled test email and exit (nothing is logged)")
    args = parser.parse_args()

    if args.send_test:
        notify.send_email(notify.build_email([{
            "severity": "warning", "rule_name": "test",
            "message": "TEST - email delivery is working. No market condition triggered this.",
        }]))
        log.info("test email sent")
        return

    with connect() as conn:
        alerts = evaluate(conn, args.as_of)
        for a in alerts:
            log.info("%-8s %s", a.severity.upper(), a.message)
        if args.dry_run:
            log.info("dry run: %d alert(s) would be evaluated; nothing written", len(alerts))
            return
        new = record(conn, alerts, args.as_of)
        status, n = deliver(conn)
    log.info("%d condition(s) met, %d new alert(s) logged, delivery: %s (%d)",
             len(alerts), new, status, n)


if __name__ == "__main__":
    main()
