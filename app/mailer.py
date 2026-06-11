"""Stdlib-only SMTP sender for the daily digest.

Reads config from environment variables:

    SMTP_HOST      (required)  smtp.gmail.com / smtp.qq.com / ...
    SMTP_PORT      (default 587)
    SMTP_USER      (required)  login user, also default From address
    SMTP_PASS      (required)  app password / authorization code
    SMTP_FROM      (default = SMTP_USER)
    SMTP_TO        (required)  comma-separated recipient list
    SMTP_USE_TLS   (default true)   true → STARTTLS on 587; false → SMTP_SSL on 465
    SMTP_TIMEOUT   (default 30)     connect/read timeout in seconds

No third-party deps — only the Python stdlib (smtplib + email.*).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid


REQUIRED_ENV = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_TO")


def _truthy(s: str | None, default: bool) -> bool:
    if s is None or s == "":
        return default
    return s.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def is_configured() -> bool:
    """True iff every required SMTP_* env var is set to a non-empty value."""
    return all((os.getenv(name) or "").strip() for name in REQUIRED_ENV)


def _parse_recipients(raw: str) -> list[str]:
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def send_mail(subject: str, html: str, text: str) -> None:
    """Send a multipart/alternative email. Raises on failure.

    The plain-text part is attached BEFORE the HTML part, per RFC 2046 §5.1.4
    (the LAST attached alternative is the one the client should prefer).
    """
    host = os.getenv("SMTP_HOST", "").strip()
    port = int((os.getenv("SMTP_PORT") or "587").strip())
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    from_addr = (os.getenv("SMTP_FROM") or user).strip() or user
    to_raw = os.getenv("SMTP_TO", "").strip()
    use_tls = _truthy(os.getenv("SMTP_USE_TLS"), True)
    timeout = float((os.getenv("SMTP_TIMEOUT") or "30").strip())

    if not (host and user and password and to_raw):
        missing = [n for n in REQUIRED_ENV if not (os.getenv(n) or "").strip()]
        raise RuntimeError(f"SMTP not configured: missing {', '.join(missing)}")

    recipients = _parse_recipients(to_raw)
    if not recipients:
        raise RuntimeError("SMTP_TO has no valid addresses")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("TrendRadarCN", from_addr))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or "trendradar")

    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if use_tls:
        # STARTTLS path (typically port 587).
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(user, password)
            smtp.sendmail(from_addr, recipients, msg.as_string())
    else:
        # Implicit-SSL path (typically port 465).
        with smtplib.SMTP_SSL(
            host, port, timeout=timeout, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(user, password)
            smtp.sendmail(from_addr, recipients, msg.as_string())
