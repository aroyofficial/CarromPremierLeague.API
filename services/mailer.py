"""HTML mail for admin accounts.

Templates keep {{placeholder}} names. Values are escaped before they are
inserted, except the prebuilt details table. CSS animation is included for
clients that keep a style block; the layout still reads when that is removed.
"""

import base64
import html
import logging
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from core.config import settings

logger = logging.getLogger("cpl.mail")

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
_SAFE = {"details"}


class MailError(Exception):
    pass


@dataclass
class OutboundMail:
    to: str
    subject: str
    html: str
    text: str


def fill(template: str, values: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        raw = "" if values.get(key) is None else str(values.get(key))
        return raw if key in _SAFE else html.escape(raw)

    return _PLACEHOLDER.sub(replace, template)


def _details(rows: list[tuple[str, str]]) -> str:
    cells = []
    for label, value in rows:
        if not value:
            continue
        cells.append(
            "<tr>"
            f"<td style=\"padding:8px 0;color:#64748b;font-size:13px;width:140px;\">{html.escape(label)}</td>"
            f"<td style=\"padding:8px 0;color:#0f172a;font-size:15px;font-weight:700;\">{html.escape(value)}</td>"
            "</tr>"
        )
    if not cells:
        return ""
    return (
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        "style=\"margin:18px 0;border-top:1px solid #e7e1d4;border-bottom:1px solid #e7e1d4;\">"
        + "".join(cells)
        + "</table>"
    )


_LAYOUT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{subject}}</title>
<style>
  @keyframes cpl-rise { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: none; } }
  @keyframes cpl-shimmer { 0% { background-position: 0% 50%; } 100% { background-position: 220% 50%; } }
  @keyframes cpl-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.05); } }
  .cpl-card { animation: cpl-rise 0.7s ease both; }
  .cpl-bar { background-size: 220% 100%; animation: cpl-shimmer 3.6s linear infinite; }
  .cpl-badge { animation: cpl-pulse 2.8s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) {
    .cpl-card, .cpl-bar, .cpl-badge { animation: none; }
  }
</style>
</head>
<body style="margin:0;padding:0;background:#f6f1e7;font-family:Georgia, 'Segoe UI', sans-serif;color:#0f172a;">
  <div style="display:none;max-height:0;overflow:hidden;">{{preheader}}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f1e7;padding:28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" class="cpl-card" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:22px;overflow:hidden;border:1px solid #eadfca;box-shadow:0 18px 40px rgba(15,23,42,0.08);">
          <tr>
            <td class="cpl-bar" style="height:8px;background-image:linear-gradient(90deg,#f5b82e,#fff1c2,#f5b82e);background-color:#f5b82e;"></td>
          </tr>
          <tr>
            <td style="padding:28px 28px 8px;background:#0b1426;">
              <div class="cpl-badge" style="width:54px;height:54px;border-radius:16px;background:#f5b82e;color:#0b1426;font-weight:800;font-size:16px;line-height:54px;text-align:center;font-family:'Segoe UI',sans-serif;">CPL</div>
              <p style="margin:16px 0 0;color:#f5b82e;font-size:12px;letter-spacing:0.14em;text-transform:uppercase;font-family:'Segoe UI',sans-serif;">{{league_name}}</p>
              <h1 style="margin:6px 0 0;color:#ffffff;font-size:26px;line-height:1.25;font-weight:800;">{{headline}}</h1>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 28px 8px;font-size:16px;line-height:1.55;color:#334155;">
              <p style="margin:0 0 12px;">Hello {{given_name}},</p>
              <p style="margin:0;">{{intro}}</p>
              {{details}}
              <p style="margin:0 0 18px;">{{note}}</p>
              <a href="{{login_url}}" style="display:inline-block;background:#0b1426;color:#ffffff;text-decoration:none;font-family:'Segoe UI',sans-serif;font-weight:700;font-size:15px;padding:12px 18px;border-radius:999px;">{{action}}</a>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 28px 28px;color:#94a3b8;font-size:12px;line-height:1.5;font-family:'Segoe UI',sans-serif;">
              This message was sent to {{email}} for the {{username}} admin account. If you were not expecting it, you can ignore it.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


_CODE_COPY = {
    "email_change": (
        "Confirm your admin email",
        "Confirm this email address",
        "Enter this code to save the new email address on the admin account. It expires in 10 minutes.",
    ),
    "password_reset": (
        "Reset your admin password",
        "Reset your password",
        "Enter this code on the sign-in page with the username for this account. It expires in 10 minutes.",
    ),
    "password_change": (
        "Confirm your new admin password",
        "Confirm the password change",
        "Enter this code to confirm the new password for this admin account. It expires in 10 minutes.",
    ),
    "activation": (
        "Verify your admin email",
        "Verify your email to activate the account",
        "Enter this code to verify this email address. After you set your own password, the account is activated. It expires in 10 minutes.",
    ),
    "login": (
        "Your admin sign-in code",
        "Finish signing in",
        "Enter this code on the sign-in page to finish signing in. It expires in 10 minutes.",
    ),
}


def _given_name(first_name: str | None, username: str) -> str:
    return first_name.strip() if first_name and first_name.strip() else username


def _contact(phone_code: str | None, phone_number: str | None) -> str:
    return " ".join(part for part in (phone_code, phone_number) if part)


class Mailer:
    configured = True

    def send(self, mail: OutboundMail) -> None:
        raise NotImplementedError

    def welcome(self, admin, password: str) -> OutboundMail:
        contact = _contact(admin.phone_code, admin.phone_number)
        values = self._base(admin, "Your admin account is ready")
        values.update({
            "headline": "Your admin account is ready",
            "preheader": "A temporary password is waiting. Sign in and change it to activate the account.",
            "intro": "An administrator created this account. The password below was generated by the system. It is a default password, not one you chose.",
            "details": _details([
                ("Username", admin.username),
                ("Email", admin.email or ""),
                ("Contact number", contact),
                ("Temporary password", password),
            ]),
            "note": "The account stays switched off until you sign in and change this password. After you change it, the account is activated.",
            "action": "Sign in and change it",
        })
        text = (
            f"Hello {values['given_name']},\n\n"
            "This is the default password generated by the system. "
            "Sign in and change it. The account stays off until you do.\n\n"
            f"Username: {admin.username}\n"
            f"Email: {admin.email}\n"
            f"Contact number: {contact}\n"
            f"Temporary password: {password}\n\n"
            f"Sign in: {values['login_url']}\n"
        )
        return self._deliver(admin.email, values["subject"], values, text)

    def password_changed(self, admin, activated: bool, via: str = "profile") -> OutboundMail:
        values = self._base(admin, "Your admin password was changed")
        changed_at = getattr(admin, "password_changed_at", None) or datetime.now(timezone.utc)
        when = f"{changed_at.strftime('%d %b %Y, %H:%M')} UTC"
        how = "Reset with an email code from the sign-in page" if via == "reset" else "Changed from My profile after an email code"
        if activated:
            values.update({
                "headline": "Your account is now active",
                "preheader": "The temporary password has been replaced and the account can sign in.",
                "intro": "You replaced the default password generated by the system. This account is now activated.",
                "note": "If you did not make this change, reset the password from the sign-in page.",
            })
        else:
            values.update({
                "headline": "Your password was changed",
                "preheader": "The password for your admin account was just changed.",
                "intro": "The password for this admin account was changed. Existing sign-ins have been signed out.",
                "note": "If you did not expect this, reset the password from the sign-in page.",
            })
        values["details"] = _details([
            ("Username", admin.username),
            ("When", when),
            ("How", how),
        ])
        values["action"] = "Open the sign-in page"
        text = (
            f"Hello {values['given_name']},\n\n{values['intro']}\n\n"
            f"Username: {admin.username}\nWhen: {when}\nHow: {how}\n\n"
            f"{values['note']}\n{values['login_url']}\n"
        )
        return self._deliver(admin.email, values["subject"], values, text)

    def code(self, admin, purpose: str, code: str, destination: str) -> OutboundMail:
        subject, headline, intro = _CODE_COPY.get(purpose, _CODE_COPY["password_reset"])
        preheader = f"Your code is {code}."
        values = self._base(admin, subject)
        values.update({
            "email": destination,
            "headline": headline,
            "preheader": preheader,
            "intro": intro,
            "details": _details([("Username", admin.username), ("Code", code)]),
            "note": "If you did not ask for this code, you can ignore this message.",
            "action": "Open the sign-in page",
        })
        text = (
            f"Hello {values['given_name']},\n\n{intro}\n\n"
            f"Username: {admin.username}\nCode: {code}\n\n{values['login_url']}\n"
        )
        return self._deliver(destination, subject, values, text)

    def _base(self, admin, subject: str) -> dict:
        return {
            "subject": subject,
            "league_name": "Carrom Premier League",
            "given_name": _given_name(getattr(admin, "first_name", None), admin.username),
            "username": admin.username,
            "email": admin.email or "",
            "login_url": f"{settings.PUBLIC_WEB_URL}/login",
            "details": "",
            "note": "",
            "action": "Open the sign-in page",
            "preheader": subject,
            "headline": subject,
            "intro": "",
        }

    def _deliver(self, to: str, subject: str, values: dict, text: str) -> OutboundMail:
        mail = OutboundMail(to=to, subject=subject, html=fill(_LAYOUT, values), text=text)
        self.send(mail)
        return mail


class MemoryMailer(Mailer):
    def __init__(self):
        self.sent: list[OutboundMail] = []

    def send(self, mail: OutboundMail) -> None:
        self.sent.append(mail)


def _mime(mail: OutboundMail, sender: str, name: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((name, sender))
    message["To"] = mail.to
    message["Subject"] = mail.subject
    message.set_content(mail.text)
    message.add_alternative(mail.html, subtype="html")
    return message


class SmtpMailer(Mailer):
    """Direct SMTP. Render's free plan blocks ports 25, 465 and 587, so use an HTTPS transport there."""

    transport = "smtp"
    defer = True

    def __init__(self, config=None):
        self._settings = config or settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.SMTP_HOST and self._settings.MAIL_FROM)

    def send(self, mail: OutboundMail) -> None:
        if not self.configured:
            raise MailError("Email is not configured.")
        message = _mime(mail, self._settings.MAIL_FROM, self._settings.SMTP_FROM_NAME)
        host, port = self._settings.SMTP_HOST, self._settings.SMTP_PORT
        try:
            if port == 465:
                client = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=20)
            with client:
                if port != 465 and self._settings.SMTP_STARTTLS:
                    client.starttls(context=ssl.create_default_context())
                if self._settings.SMTP_USERNAME:
                    client.login(self._settings.SMTP_USERNAME, self._settings.SMTP_PASSWORD or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            logger.warning("SMTP delivery to %s:%s failed: %s: %s", host, port, type(error).__name__, error)
            raise MailError("The email could not be sent.") from error


class HttpMailer(Mailer):
    """Sends through an email provider's HTTPS API, which works where outbound SMTP is blocked."""

    defer = True

    def __init__(self, provider: str, config=None, client: httpx.Client | None = None):
        self.transport = provider
        self._settings = config or settings
        self._client = client
        self._gmail_token: tuple[str, float] | None = None

    @property
    def configured(self) -> bool:
        if not self._settings.MAIL_FROM:
            return False
        if self.transport == "gmail":
            return bool(
                self._settings.GMAIL_CLIENT_ID
                and self._settings.GMAIL_CLIENT_SECRET
                and self._settings.GMAIL_REFRESH_TOKEN
            )
        return bool(self._settings.MAIL_API_KEY)

    def send(self, mail: OutboundMail) -> None:
        if not self.configured:
            raise MailError("Email is not configured.")
        try:
            response = getattr(self, f"_send_{self.transport}")(mail)
        except httpx.HTTPError as error:
            logger.warning("%s delivery failed: %s: %s", self.transport, type(error).__name__, error)
            raise MailError("The email could not be sent.") from error
        if response.status_code >= 300:
            logger.warning(
                "%s rejected the email (HTTP %s): %s", self.transport, response.status_code, response.text[:300]
            )
            raise MailError("The email could not be sent.")

    def _post(self, url: str, **kwargs) -> httpx.Response:
        if self._client is not None:
            return self._client.post(url, **kwargs)
        with httpx.Client(timeout=20) as client:
            return client.post(url, **kwargs)

    def _sender(self) -> tuple[str, str]:
        return self._settings.MAIL_FROM, self._settings.SMTP_FROM_NAME

    def _send_brevo(self, mail: OutboundMail) -> httpx.Response:
        sender, name = self._sender()
        return self._post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": self._settings.MAIL_API_KEY, "accept": "application/json"},
            json={
                "sender": {"email": sender, "name": name},
                "to": [{"email": mail.to}],
                "subject": mail.subject,
                "htmlContent": mail.html,
                "textContent": mail.text,
            },
        )

    def _send_resend(self, mail: OutboundMail) -> httpx.Response:
        sender, name = self._sender()
        return self._post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self._settings.MAIL_API_KEY}"},
            json={
                "from": formataddr((name, sender)),
                "to": [mail.to],
                "subject": mail.subject,
                "html": mail.html,
                "text": mail.text,
            },
        )

    def _send_sendgrid(self, mail: OutboundMail) -> httpx.Response:
        sender, name = self._sender()
        return self._post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {self._settings.MAIL_API_KEY}"},
            json={
                "personalizations": [{"to": [{"email": mail.to}]}],
                "from": {"email": sender, "name": name},
                "subject": mail.subject,
                "content": [
                    {"type": "text/plain", "value": mail.text},
                    {"type": "text/html", "value": mail.html},
                ],
            },
        )

    def _send_gmail(self, mail: OutboundMail) -> httpx.Response:
        sender, name = self._sender()
        raw = base64.urlsafe_b64encode(_mime(mail, sender, name).as_bytes()).decode("ascii")
        return self._post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {self._gmail_access_token()}"},
            json={"raw": raw},
        )

    def _gmail_access_token(self) -> str:
        if self._gmail_token and self._gmail_token[1] > time.time() + 60:
            return self._gmail_token[0]
        response = self._post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self._settings.GMAIL_CLIENT_ID,
                "client_secret": self._settings.GMAIL_CLIENT_SECRET,
                "refresh_token": self._settings.GMAIL_REFRESH_TOKEN,
            },
        )
        if response.status_code >= 300:
            logger.warning("Google refused the Gmail refresh token (HTTP %s): %s", response.status_code, response.text[:300])
            raise MailError("The email could not be sent.")
        body = response.json()
        self._gmail_token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._gmail_token[0]


_KEY_PREFIXES = (("xkeysib-", "brevo"), ("re_", "resend"), ("SG.", "sendgrid"))
HTTP_PROVIDERS = ("brevo", "resend", "sendgrid", "gmail")


def resolve_provider(config=None) -> str:
    config = config or settings
    if config.MAIL_PROVIDER in HTTP_PROVIDERS + ("smtp",):
        return config.MAIL_PROVIDER
    if config.GMAIL_REFRESH_TOKEN:
        return "gmail"
    if config.MAIL_API_KEY:
        for prefix, provider in _KEY_PREFIXES:
            if config.MAIL_API_KEY.startswith(prefix):
                return provider
    return "smtp"


_shared: dict[str, Mailer] = {}


def get_mailer() -> Mailer:
    provider = resolve_provider()
    if provider not in _shared:
        _shared[provider] = SmtpMailer() if provider == "smtp" else HttpMailer(provider)
    return _shared[provider]
