"""Email delivery over SMTP (STARTTLS). One digest email per run, plain text + HTML."""

import os
import smtplib
from email.message import EmailMessage
from html import escape

from core.config import env, settings

SEVERITY_COLORS = {"critical": "#b42318", "warning": "#b54708"}


def email_configured() -> bool:
    return all(os.getenv(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"))


def build_email(alerts: list[dict]) -> EmailMessage:
    """`alerts` are rows from the alerts table (dicts with rule_name, severity, message, ...)."""
    n = len(alerts)
    worst = "critical" if any(a["severity"] == "critical" for a in alerts) else "warning"
    msg = EmailMessage()
    msg["Subject"] = f"[Gas Storage Monitor] {n} {worst} alert{'s' if n > 1 else ''}: " + (
        alerts[0]["message"] if n == 1 else alerts[0]["message"][:80] + " ..."
    )
    msg["From"] = env("ALERT_EMAIL_FROM", os.getenv("SMTP_USER"))
    msg["To"] = env("ALERT_EMAIL_TO")

    dashboard = settings()["alerts"].get("dashboard_url")
    text = [f"{a['severity'].upper():8} {a['message']}" for a in alerts]
    if dashboard:
        text += ["", f"Dashboard: {dashboard}"]
    msg.set_content("\n".join(text))

    rows = "".join(
        f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #eaecf0;white-space:nowrap">
                <span style="color:#fff;background:{SEVERITY_COLORS[a['severity']]};
                  border-radius:4px;padding:2px 8px;font-size:12px">{a['severity']}</span></td>
              <td style="padding:8px 12px;border-bottom:1px solid #eaecf0">
                {escape(a['message'])}</td>
            </tr>"""
        for a in alerts
    )
    link = (f'<p style="margin-top:16px"><a href="{escape(dashboard)}">Open the dashboard</a></p>'
            if dashboard else "")
    msg.add_alternative(
        f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                        color:#101828;max-width:680px">
              <h2 style="font-size:18px;margin:0 0 12px">Gas Storage Monitor</h2>
              <table style="border-collapse:collapse;width:100%;font-size:14px">{rows}</table>
              {link}
              <p style="color:#667085;font-size:12px;margin-top:16px">
                Thresholds are set in config/settings.yaml. Sources: EIA, NOAA CPC.</p>
            </div>""",
        subtype="html",
    )
    return msg


def send_email(msg: EmailMessage) -> None:
    host, port = env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
        smtp.send_message(msg)
