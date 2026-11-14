"""Outgoing email.

EMAIL_BACKEND picks how messages go out:

- console (default): printed in the terminal instead of sent, for local development.
- brevo: sent through Brevo's HTTPS API with BREVO_API_KEY, from EMAIL_FROM (a sender
  address verified in Brevo). It doesn't use SMTP, which Render's free plan blocks.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import requests

from app.config import Settings, get_settings

log = logging.getLogger("payoff.mail")
BACKENDS = ("console", "brevo")
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


@dataclass
class Message:
    to: str
    subject: str
    text: str
    html: str


class EmailError(RuntimeError):
    pass


def send(msg: Message) -> None:
    """Sends now. Raises EmailError if it can't."""
    settings = get_settings()
    backend = settings.email_backend.strip().lower()
    if backend == "console":
        _console(msg)
    elif backend == "brevo":
        _brevo(msg, settings)
    else:
        raise EmailError(f"EMAIL_BACKEND={settings.email_backend!r} isn't supported; use {' or '.join(BACKENDS)}.")


def send_logged(msg: Message) -> None:
    """For background tasks, where the response has already gone: logs failures instead."""
    try:
        send(msg)
    except Exception as err:  # noqa: BLE001 - nothing upstream can handle it
        log.error("Couldn't email %s (%s): %s", msg.to, msg.subject, err)


def config_problems(settings: Settings) -> list[str]:
    """Settings that will stop emails reaching people, for a warning at startup."""
    backend = settings.email_backend.strip().lower()
    problems = []
    if backend not in BACKENDS:
        problems.append(f"EMAIL_BACKEND={settings.email_backend!r} isn't supported; use {' or '.join(BACKENDS)}.")
    if backend == "brevo" and not (settings.brevo_api_key and settings.email_from):
        problems.append("EMAIL_BACKEND=brevo needs BREVO_API_KEY and EMAIL_FROM; emails will fail.")
    if backend == "brevo" and settings.brevo_api_key.startswith("xsmtpsib-"):
        problems.append("BREVO_API_KEY is an SMTP key. Create an API key instead (it starts with xkeysib-).")
    if settings.cookie_secure and backend == "console":
        problems.append("EMAIL_BACKEND is console: password-reset emails are printed to this log, not sent.")
    return problems


def _console(msg: Message) -> None:
    rule = "-" * 60
    print(f"\n{rule}\nEmail (EMAIL_BACKEND=console, not sent)\nTo: {msg.to}\nSubject: {msg.subject}\n\n"
          f"{msg.text.strip()}\n{rule}\n", flush=True)


def _brevo(msg: Message, settings: Settings) -> None:
    if not settings.brevo_api_key or not settings.email_from:
        raise EmailError("Set BREVO_API_KEY and EMAIL_FROM to send email with Brevo.")
    payload = {
        "sender": {"name": settings.email_from_name.strip() or "Payoff", "email": settings.email_from},
        "to": [{"email": msg.to}],
        "subject": msg.subject,
        "textContent": msg.text,
        "htmlContent": msg.html,
    }
    try:
        r = requests.post(BREVO_URL, json=payload, timeout=15,
                          headers={"api-key": settings.brevo_api_key, "accept": "application/json"})
    except requests.RequestException as err:
        raise EmailError(f"couldn't reach Brevo: {err}") from err
    if r.status_code >= 300:
        # Brevo answers {"code": ..., "message": ...}; a 401 is a wrong key or an IP it hasn't authorised.
        raise EmailError(f"Brevo answered {r.status_code}: {r.text[:300]}")


# ---- messages ------------------------------------------------------------------------------
def password_reset(to: str, link: str, minutes: int) -> Message:
    text = (
        f"Someone asked to reset the password for the Payoff account {to}.\n\n"
        f"Choose a new password here. The link works once and expires in {minutes} minutes:\n"
        f"{link}\n\n"
        "If you didn't ask for this, you can ignore this email. Your password won't change.\n"
    )
    e_to, e_link = html.escape(to), html.escape(link, quote=True)
    body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;line-height:1.5;color:#1b2430;max-width:480px">
  <p>Someone asked to reset the password for the Payoff account <b>{e_to}</b>.</p>
  <p><a href="{e_link}" style="display:inline-block;padding:10px 18px;background:#1f5fbf;color:#ffffff;border-radius:6px;text-decoration:none;font-weight:600">Choose a new password</a></p>
  <p style="color:#5b6573;font-size:13px">The link works once and expires in {minutes} minutes. If the button doesn't work, paste this address into your browser:<br><a href="{e_link}" style="color:#1f5fbf;word-break:break-all">{e_link}</a></p>
  <p style="color:#5b6573;font-size:13px">If you didn't ask for this, you can ignore this email. Your password won't change.</p>
</div>"""
    return Message(to=to, subject="Reset your Payoff password", text=text, html=body)


def setup_check(to: str) -> Message:
    text = "This is a test email from your Payoff server. If you can read it, email is set up.\n"
    return Message(to=to, subject="Payoff test email", text=text, html=f"<p>{html.escape(text.strip())}</p>")
