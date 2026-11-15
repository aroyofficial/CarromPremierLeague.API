"""HTML mail for admin accounts.

Every message shares one table-based layout with inline styles, so it reads the
same in Gmail, Outlook and Apple Mail. Outfit and Poppins load where the client
allows web fonts; the stacks fall back to Segoe UI, Roboto and Arial elsewhere.
Every value is escaped before it is placed in the HTML.
"""

import base64
import html
import logging
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


class MailError(Exception):
    pass


@dataclass
class OutboundMail:
    to: str
    subject: str
    html: str
    text: str


_BODY_FONT = "'Poppins','Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_DISPLAY_FONT = "'Outfit','Poppins','Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO_FONT = "'JetBrains Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace"

_NAVY = "#0b1426"
_NAVY_SOFT = "#18294d"
_GOLD = "#f5b82e"
_GOLD_SOFT = "#fff6dc"
_INK = "#0f172a"
_INK_MUTED = "#475569"
_INK_FAINT = "#94a3b8"
_LINE = "#e2e8f0"
_PAPER = "#f8fafc"

_TONES = {
    "gold": ("#8a5a00", _GOLD_SOFT),
    "green": ("#17643a", "#e8f7ee"),
    "navy": (_NAVY_SOFT, "#e8ecf5"),
}

_CODE_MINUTES = 10


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _eyebrow(label: str, tone: str) -> str:
    color, background = _TONES[tone]
    return (
        f'<span style="display:inline-block;padding:5px 12px;border-radius:999px;background:{background};'
        f'color:{color};font-family:{_BODY_FONT};font-size:11px;font-weight:600;letter-spacing:0.12em;'
        f'text-transform:uppercase;">{_e(label)}</span>'
    )


def _paragraph(text: str, *, size: int = 15, color: str = _INK_MUTED, margin: str = "0 0 16px") -> str:
    if not text:
        return ""
    return (
        f'<p style="margin:{margin};font-family:{_BODY_FONT};font-size:{size}px;line-height:1.65;color:{color};">'
        f"{_e(text)}</p>"
    )


def _code_block(code: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 22px;">'
        f'<tr><td align="center" style="padding:22px 12px 18px;border-radius:16px;background:{_GOLD_SOFT};'
        f'border:1px solid #f6dc9b;">'
        f'<div style="font-family:{_BODY_FONT};font-size:11px;font-weight:600;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:#8a5a00;">Your code</div>'
        f'<div class="cpl-code" style="margin:8px 0 6px;font-family:{_DISPLAY_FONT};font-size:38px;'
        f'font-weight:700;letter-spacing:0.32em;padding-left:0.32em;color:{_NAVY};">{_e(code)}</div>'
        f'<div style="font-family:{_BODY_FONT};font-size:13px;color:{_INK_MUTED};">'
        f"Expires in {_CODE_MINUTES} minutes</div>"
        "</td></tr></table>"
    )


def _details(rows: list[tuple[str, str, bool]]) -> str:
    cells = []
    for label, value, secret in rows:
        if not value:
            continue
        if secret:
            shown = (
                f'<span style="display:inline-block;padding:6px 10px;border-radius:8px;border:1px dashed #d4a72c;'
                f'background:#ffffff;font-family:{_MONO_FONT};font-size:15px;font-weight:600;color:{_NAVY};'
                f'letter-spacing:0.04em;word-break:break-all;">{_e(value)}</span>'
            )
        else:
            shown = (
                f'<span style="font-family:{_BODY_FONT};font-size:15px;font-weight:600;color:{_INK};'
                f'word-break:break-word;">{_e(value)}</span>'
            )
        top = "" if not cells else f"border-top:1px solid {_LINE};"
        cells.append(
            "<tr>"
            f'<td class="cpl-label" style="{top}padding:12px 14px 12px 0;width:40%;vertical-align:top;font-family:{_BODY_FONT};'
            f'font-size:13px;color:{_INK_MUTED};">{_e(label)}</td>'
            f'<td style="{top}padding:12px 0;vertical-align:top;">{shown}</td>'
            "</tr>"
        )
    if not cells:
        return ""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:4px 0 22px;">'
        f'<tr><td style="padding:4px 18px;border-radius:14px;background:{_PAPER};border:1px solid {_LINE};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(cells)
        + "</table></td></tr></table>"
    )


def _steps(items: list[tuple[str, str]]) -> str:
    rows = []
    for index, (title, text) in enumerate(items, start=1):
        rows.append(
            "<tr>"
            '<td style="width:40px;padding:0 0 14px;vertical-align:top;">'
            f'<div style="width:28px;height:28px;border-radius:50%;background:{_NAVY};color:{_GOLD};'
            f'font-family:{_DISPLAY_FONT};font-size:14px;font-weight:700;line-height:28px;text-align:center;">{index}</div>'
            "</td>"
            '<td style="padding:2px 0 14px;vertical-align:top;">'
            f'<div style="font-family:{_BODY_FONT};font-size:15px;font-weight:600;color:{_INK};">{_e(title)}</div>'
            f'<div style="font-family:{_BODY_FONT};font-size:13px;line-height:1.55;color:{_INK_MUTED};">{_e(text)}</div>'
            "</td>"
            "</tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 10px;">'
        + "".join(rows)
        + "</table>"
    )


def _callout(title: str, text: str, tone: str = "warn") -> str:
    border, background, heading = {
        "warn": ("#f4c7c3", "#fff5f4", "#b42318"),
        "quiet": (_LINE, _PAPER, _INK),
    }[tone]
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:22px 0 0;">'
        f'<tr><td style="padding:14px 16px;border-radius:12px;background:{background};border:1px solid {border};">'
        f'<div style="font-family:{_BODY_FONT};font-size:14px;font-weight:600;color:{heading};">{_e(title)}</div>'
        f'<div style="margin-top:4px;font-family:{_BODY_FONT};font-size:13px;line-height:1.55;color:{_INK_MUTED};">'
        f"{_e(text)}</div>"
        "</td></tr></table>"
    )


def _button(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" class="cpl-btn" style="margin:6px 0 0;">'
        f'<tr><td align="center" style="border-radius:12px;background:{_NAVY};">'
        f'<a href="{_e(url)}" target="_blank" style="display:inline-block;padding:14px 26px;border-radius:12px;'
        f'font-family:{_BODY_FONT};font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">'
        f'{_e(label)}&nbsp;&nbsp;<span style="color:{_GOLD};">&rarr;</span></a>'
        "</td></tr></table>"
    )


def _page(*, subject: str, preheader: str, body: str, recipient: str, username: str) -> str:
    year = datetime.now(timezone.utc).year
    site = settings.PUBLIC_WEB_URL
    logo = settings.MAIL_LOGO_URL
    padding = "&#8204;&nbsp;" * 60
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{_e(subject)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@600;700;800&family=Poppins:wght@400;500;600&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<!--[if mso]><style>body,table,td,div,p,a,span,h1{{font-family:'Segoe UI',Arial,sans-serif !important;}}</style><![endif]-->
<style>
  body {{ margin:0; padding:0; -webkit-text-size-adjust:100%; }}
  a {{ color:{_NAVY_SOFT}; }}
  @media (max-width: 600px) {{
    .cpl-shell {{ padding:16px 10px !important; }}
    .cpl-pad {{ padding-left:22px !important; padding-right:22px !important; }}
    .cpl-h1 {{ font-size:24px !important; }}
    .cpl-code {{ font-size:32px !important; letter-spacing:0.24em !important; padding-left:0.24em !important; }}
    .cpl-label {{ width:38% !important; }}
    .cpl-btn, .cpl-btn tbody, .cpl-btn tr, .cpl-btn td, .cpl-btn a {{ display:block !important; width:100% !important; text-align:center !important; box-sizing:border-box; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#eef1f6;">
<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all;">{_e(preheader)}{padding}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f6;">
  <tr>
    <td align="center" class="cpl-shell" style="padding:32px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:580px;">
        <tr>
          <td class="cpl-pad" style="padding:22px 36px;border-radius:20px 20px 0 0;background:{_NAVY};border-bottom:4px solid {_GOLD};">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:56px;">
                  <img src="{_e(logo)}" width="52" height="52" alt="CPL" style="display:block;width:52px;height:52px;border-radius:50%;border:2px solid {_GOLD};background:#ffffff;">
                </td>
                <td style="padding-left:14px;">
                  <div style="font-family:{_DISPLAY_FONT};font-size:18px;font-weight:700;color:#ffffff;line-height:1.2;">Carrom Premier League</div>
                  <div style="margin-top:3px;font-family:{_BODY_FONT};font-size:11px;font-weight:600;letter-spacing:0.16em;text-transform:uppercase;color:{_GOLD};">Admin console</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td class="cpl-pad" style="padding:34px 36px 30px;background:#ffffff;">
            {body}
          </td>
        </tr>
        <tr>
          <td class="cpl-pad" style="padding:18px 36px 22px;background:#ffffff;border-top:1px solid {_LINE};border-radius:0 0 20px 20px;">
            <p style="margin:0;font-family:{_BODY_FONT};font-size:12px;line-height:1.6;color:{_INK_FAINT};">
              Sent to {_e(recipient)} for the <strong style="color:{_INK_MUTED};font-weight:600;">{_e(username)}</strong> admin account.
              If you were not expecting this email, you can ignore it.
            </p>
          </td>
        </tr>
        <tr>
          <td align="center" style="padding:20px 12px 0;">
            <p style="margin:0;font-family:{_BODY_FONT};font-size:12px;line-height:1.6;color:{_INK_FAINT};">
              &copy; {year} Carrom Premier League &middot; <a href="{_e(site)}" target="_blank" style="color:{_INK_MUTED};text-decoration:underline;">Visit the league site</a>
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""


def _heading(eyebrow: str, tone: str, headline: str) -> str:
    return (
        f"{_eyebrow(eyebrow, tone)}"
        f'<h1 class="cpl-h1" style="margin:14px 0 18px;font-family:{_DISPLAY_FONT};font-size:28px;line-height:1.25;'
        f'font-weight:700;color:{_INK};">{_e(headline)}</h1>'
    )


@dataclass(frozen=True)
class _CodeCopy:
    subject: str
    eyebrow: str
    headline: str
    intro: str
    where: str


_CODE_COPY = {
    "email_change": _CodeCopy(
        "Confirm your admin email",
        "Email change",
        "Confirm this email address",
        "Enter this code to save the new email address on the admin account.",
        "Enter it in My profile, where you changed the email.",
    ),
    "password_reset": _CodeCopy(
        "Reset your admin password",
        "Password reset",
        "Reset your password",
        "Enter this code on the sign-in page with the username for this account.",
        "Then choose a new password. The code works once.",
    ),
    "password_change": _CodeCopy(
        "Confirm your new admin password",
        "Password change",
        "Confirm it's you",
        "Enter this code to confirm the new password for this admin account.",
        "Enter it in My profile, then choose the new password.",
    ),
    "activation": _CodeCopy(
        "Verify your admin email",
        "Account activation",
        "Verify your email",
        "Enter this code to verify this email address. After you set your own password, the account is activated.",
        "Enter it on the activation screen in the admin console.",
    ),
    "login": _CodeCopy(
        "Your admin sign-in code",
        "Sign-in code",
        "Finish signing in",
        "Enter this code on the sign-in page to finish signing in.",
        "It only works for the sign-in you just started.",
    ),
}


def _given_name(first_name: str | None, username: str) -> str:
    return first_name.strip() if first_name and first_name.strip() else username


def _contact(phone_code: str | None, phone_number: str | None) -> str:
    return " ".join(part for part in (phone_code, phone_number) if part)


def _text(given_name: str, paragraphs: list[str], rows: list[tuple[str, str]], link: str = "") -> str:
    lines = [f"Hello {given_name},", ""]
    for paragraph in paragraphs:
        if paragraph:
            lines += [paragraph, ""]
    for label, value in rows:
        if value:
            lines.append(f"{label}: {value}")
    if rows:
        lines.append("")
    if link:
        lines += [link, ""]
    lines += ["Carrom Premier League", ""]
    return "\n".join(lines)


class Mailer:
    configured = True

    def send(self, mail: OutboundMail) -> None:
        raise NotImplementedError

    def welcome(self, admin, password: str) -> OutboundMail:
        given = _given_name(getattr(admin, "first_name", None), admin.username)
        contact = _contact(admin.phone_code, admin.phone_number)
        login_url = self._login_url()
        subject = "Your admin account is ready"
        intro = (
            "An administrator created a CPL admin account for you. The password below is a default password "
            "generated by the system, not one you chose."
        )
        body = (
            _heading("Welcome aboard", "gold", "Your admin account is ready")
            + _paragraph(f"Hello {given},", color=_INK, margin="0 0 10px")
            + _paragraph(intro)
            + _details([
                ("Username", admin.username, False),
                ("Email", admin.email or "", False),
                ("Contact number", contact, False),
                ("Temporary password", password, True),
            ])
            + f'<div style="margin:0 0 12px;font-family:{_DISPLAY_FONT};font-size:16px;font-weight:700;color:{_INK};">'
            "Three steps to switch it on</div>"
            + _steps([
                ("Sign in", "Use the username and temporary password above."),
                ("Verify your email", f"We email a 6-digit code. It expires in {_CODE_MINUTES} minutes."),
                ("Choose your own password", "The account turns on as soon as it is saved."),
            ])
            + _button("Sign in to activate", login_url)
            + _callout(
                "Keep this email private",
                "Anyone with the temporary password can start the activation. It stops working once you change it.",
                "quiet",
            )
        )
        text = _text(
            given,
            [
                "Your CPL admin account is ready. This is the default password generated by the system.",
                "Sign in and change it. The account stays off until you do.",
            ],
            [
                ("Username", admin.username),
                ("Email", admin.email or ""),
                ("Contact number", contact),
                ("Temporary password", password),
            ],
            f"Sign in: {login_url}",
        )
        return self._deliver(admin.email, subject, "A temporary password is waiting. Sign in and change it to activate the account.", body, text, admin.email, admin.username)

    def password_changed(self, admin, activated: bool, via: str = "profile") -> OutboundMail:
        given = _given_name(getattr(admin, "first_name", None), admin.username)
        login_url = self._login_url()
        changed_at = getattr(admin, "password_changed_at", None) or datetime.now(timezone.utc)
        when = f"{changed_at.strftime('%d %b %Y, %H:%M')} UTC"
        how = "Reset with an email code from the sign-in page" if via == "reset" else "Changed from My profile after an email code"
        subject = "Your admin password was changed"
        rows = [("Username", admin.username), ("When", when), ("How", how)]
        if activated:
            eyebrow, headline = "Account active", "Your account is now active"
            intro = "You replaced the default password generated by the system. This account is now activated."
            note = "Sign in with the new password whenever you are ready."
            preheader = "The temporary password has been replaced and the account can sign in."
            action = "Sign in"
        else:
            eyebrow, headline = "Security notice", "Your password was changed"
            intro = "The password for this admin account was just changed."
            note = "Every device that was signed in has been signed out. Sign in again with the new password."
            preheader = "The password for your admin account was just changed."
            action = "Open the sign-in page"
        body = (
            _heading(eyebrow, "green", headline)
            + _paragraph(f"Hello {given},", color=_INK, margin="0 0 10px")
            + _paragraph(intro)
            + _details([(label, value, False) for label, value in rows])
            + _paragraph(note)
            + _button(action, login_url)
            + _callout(
                "Wasn't you?",
                "Reset the password from the sign-in page right away with Forgot password, "
                "then tell another admin so they can check the account.",
            )
        )
        text = _text(
            given,
            [intro, note, "If you did not make this change, reset the password from the sign-in page."],
            rows,
            login_url,
        )
        return self._deliver(admin.email, subject, preheader, body, text, admin.email or "", admin.username)

    def code(self, admin, purpose: str, code: str, destination: str) -> OutboundMail:
        copy = _CODE_COPY.get(purpose, _CODE_COPY["password_reset"])
        given = _given_name(getattr(admin, "first_name", None), admin.username)
        body = (
            _heading(copy.eyebrow, "gold", copy.headline)
            + _paragraph(f"Hello {given},", color=_INK, margin="0 0 10px")
            + _paragraph(copy.intro)
            + _code_block(code)
            + _paragraph(copy.where, size=14)
            + _callout(
                "Never share this code",
                "CPL admins will never ask you for it. If you did not ask for this code, you can ignore this email; "
                "nothing changes without it.",
                "quiet",
            )
        )
        text = _text(
            given,
            [copy.intro, f"It expires in {_CODE_MINUTES} minutes. {copy.where}"],
            [("Username", admin.username), ("Code", code)],
        )
        return self._deliver(destination, copy.subject, f"Your code is {code}. It expires in {_CODE_MINUTES} minutes.", body, text, destination, admin.username)

    @staticmethod
    def _login_url() -> str:
        return f"{settings.PUBLIC_WEB_URL}/login"

    def _deliver(self, to: str, subject: str, preheader: str, body: str, text: str, recipient: str, username: str) -> OutboundMail:
        page = _page(subject=subject, preheader=preheader, body=body, recipient=recipient, username=username)
        mail = OutboundMail(to=to, subject=subject, html=page, text=text)
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
