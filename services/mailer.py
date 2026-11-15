"""HTML mail for admin accounts.

Templates keep {{placeholder}} names. Values are escaped before they are
inserted, except the prebuilt details table. CSS animation is included for
clients that keep a style block; the layout still reads when that is removed.
"""

import html
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from core.config import settings

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

    def password_changed(self, admin, activated: bool) -> OutboundMail:
        values = self._base(admin, "Your admin password was changed")
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
        values["details"] = _details([("Username", admin.username), ("Email", admin.email or "")])
        values["action"] = "Open the sign-in page"
        text = f"Hello {values['given_name']},\n\n{values['intro']}\n\n{values['note']}\n{values['login_url']}\n"
        return self._deliver(admin.email, values["subject"], values, text)

    def code(self, admin, purpose: str, code: str, destination: str) -> OutboundMail:
        if purpose == "email_change":
            subject = "Confirm your admin email"
            headline = "Confirm this email address"
            intro = "Enter this code to save the new email address on the admin account. It expires in 10 minutes."
            preheader = f"Your email code is {code}."
        else:
            subject = "Reset your admin password"
            headline = "Reset your password"
            intro = "Enter this code on the sign-in page with the username for this account. It expires in 10 minutes."
            preheader = f"Your reset code is {code}."
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


class SmtpMailer(Mailer):
    def __init__(self):
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.SMTP_HOST and self._settings.SMTP_FROM)

    def send(self, mail: OutboundMail) -> None:
        if not self.configured:
            raise MailError("Email is not configured.")
        message = EmailMessage()
        name = self._settings.SMTP_FROM_NAME or "Carrom Premier League"
        message["From"] = f"{name} <{self._settings.SMTP_FROM}>"
        message["To"] = mail.to
        message["Subject"] = mail.subject
        message.set_content(mail.text)
        message.add_alternative(mail.html, subtype="html")
        try:
            with smtplib.SMTP(self._settings.SMTP_HOST, self._settings.SMTP_PORT, timeout=20) as client:
                if self._settings.SMTP_STARTTLS:
                    client.starttls()
                if self._settings.SMTP_USERNAME:
                    client.login(self._settings.SMTP_USERNAME, self._settings.SMTP_PASSWORD or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise MailError("The email could not be sent.") from error
