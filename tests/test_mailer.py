import base64
import json
import os
import smtplib
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CPL_APP_NAME", "Test API")
os.environ.setdefault("CPL_APP_ENV", "dev")
os.environ.setdefault("CPL_APP_HOST", "127.0.0.1")
os.environ.setdefault("CPL_APP_PORT", "8000")
os.environ.setdefault("CPL_APP_ALLOWED_HOSTS", "http://localhost")
os.environ.setdefault("CPL_AUTH_JWT_SECRET", "test-secret-that-is-long-enough-for-unit-tests")
os.environ.setdefault("CPL_AUTH_TOKEN_EXPIRE_MINUTES", "60")
os.environ.setdefault("CPL_DB_HOST", "localhost")
os.environ.setdefault("CPL_DB_PORT", "1433")
os.environ.setdefault("CPL_DB_USER", "sa")
os.environ.setdefault("CPL_DB_PWD", "unused")
os.environ.setdefault("CPL_DB_NAME", "unused")
os.environ.setdefault("CPL_DB_POOL_SIZE", "1")

import httpx

from services.mailer import HttpMailer, MailError, OutboundMail, SmtpMailer, resolve_provider


def config(**values):
    base = dict(
        MAIL_PROVIDER="auto",
        MAIL_API_KEY=None,
        MAIL_FROM="league@example.com",
        SMTP_FROM_NAME="Carrom Premier League",
        SMTP_HOST="smtp.gmail.com",
        SMTP_PORT=587,
        SMTP_USERNAME="league@example.com",
        SMTP_PASSWORD="app-password",
        SMTP_STARTTLS=True,
        GMAIL_CLIENT_ID=None,
        GMAIL_CLIENT_SECRET=None,
        GMAIL_REFRESH_TOKEN=None,
    )
    base.update(values)
    return SimpleNamespace(**base)


MAIL = OutboundMail(to="ada@example.com", subject="Hello", html="<p>Hi</p>", text="Hi")


class Recorder:
    def __init__(self, status=200, body=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body or {}

    def client(self):
        def handle(request: httpx.Request):
            self.requests.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600})
            return httpx.Response(self.status, json=self.body)

        return httpx.Client(transport=httpx.MockTransport(handle))


class ProviderChoiceTests(unittest.TestCase):
    def test_smtp_is_the_default(self):
        self.assertEqual(resolve_provider(config()), "smtp")

    def test_api_key_prefix_picks_the_provider(self):
        self.assertEqual(resolve_provider(config(MAIL_API_KEY="xkeysib-abc")), "brevo")
        self.assertEqual(resolve_provider(config(MAIL_API_KEY="re_abc")), "resend")
        self.assertEqual(resolve_provider(config(MAIL_API_KEY="SG.abc")), "sendgrid")

    def test_gmail_refresh_token_picks_gmail(self):
        self.assertEqual(resolve_provider(config(GMAIL_REFRESH_TOKEN="refresh")), "gmail")

    def test_explicit_provider_wins(self):
        self.assertEqual(resolve_provider(config(MAIL_PROVIDER="brevo", MAIL_API_KEY="other")), "brevo")


class HttpMailerTests(unittest.TestCase):
    def test_brevo_request(self):
        recorder = Recorder(201)
        mailer = HttpMailer("brevo", config(MAIL_API_KEY="xkeysib-abc"), recorder.client())
        mailer.send(MAIL)
        request = recorder.requests[0]
        self.assertEqual(str(request.url), "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(request.headers["api-key"], "xkeysib-abc")
        body = json.loads(request.content)
        self.assertEqual(body["sender"]["email"], "league@example.com")
        self.assertEqual(body["to"], [{"email": "ada@example.com"}])
        self.assertEqual(body["htmlContent"], "<p>Hi</p>")

    def test_resend_and_sendgrid_requests(self):
        recorder = Recorder(200)
        HttpMailer("resend", config(MAIL_API_KEY="re_abc"), recorder.client()).send(MAIL)
        HttpMailer("sendgrid", config(MAIL_API_KEY="SG.abc"), recorder.client()).send(MAIL)
        resend, sendgrid = recorder.requests
        self.assertEqual(resend.headers["authorization"], "Bearer re_abc")
        self.assertIn("league@example.com", json.loads(resend.content)["from"])
        self.assertEqual(str(sendgrid.url), "https://api.sendgrid.com/v3/mail/send")
        self.assertEqual(json.loads(sendgrid.content)["personalizations"][0]["to"][0]["email"], "ada@example.com")

    def test_gmail_refreshes_a_token_then_sends_raw_mime(self):
        recorder = Recorder(200)
        mailer = HttpMailer(
            "gmail",
            config(GMAIL_CLIENT_ID="id", GMAIL_CLIENT_SECRET="secret", GMAIL_REFRESH_TOKEN="refresh"),
            recorder.client(),
        )
        mailer.send(MAIL)
        mailer.send(MAIL)
        hosts = [request.url.host for request in recorder.requests]
        self.assertEqual(hosts, ["oauth2.googleapis.com", "gmail.googleapis.com", "gmail.googleapis.com"])
        raw = json.loads(recorder.requests[1].content)["raw"]
        message = base64.urlsafe_b64decode(raw).decode()
        self.assertIn("To: ada@example.com", message)
        self.assertEqual(recorder.requests[1].headers["authorization"], "Bearer token-1")

    def test_provider_rejection_raises_mail_error(self):
        recorder = Recorder(401, {"message": "Key not found"})
        mailer = HttpMailer("brevo", config(MAIL_API_KEY="xkeysib-bad"), recorder.client())
        with self.assertLogs("cpl.mail", "WARNING") as logs, self.assertRaises(MailError):
            mailer.send(MAIL)
        self.assertIn("HTTP 401", logs.output[0])
        self.assertNotIn("xkeysib-bad", logs.output[0])

    def test_missing_key_is_not_configured(self):
        self.assertFalse(HttpMailer("brevo", config()).configured)
        with self.assertRaises(MailError):
            HttpMailer("brevo", config()).send(MAIL)


class SmtpMailerTests(unittest.TestCase):
    def test_blocked_port_is_logged_with_the_cause(self):
        with mock.patch("smtplib.SMTP", side_effect=TimeoutError("timed out")):
            with self.assertLogs("cpl.mail", "WARNING") as logs, self.assertRaises(MailError):
                SmtpMailer(config()).send(MAIL)
        self.assertIn("smtp.gmail.com:587", logs.output[0])
        self.assertIn("TimeoutError", logs.output[0])

    def test_port_465_uses_implicit_tls(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        with mock.patch("smtplib.SMTP_SSL", return_value=client) as ssl_client, mock.patch("smtplib.SMTP") as plain:
            SmtpMailer(config(SMTP_PORT=465)).send(MAIL)
        ssl_client.assert_called_once()
        plain.assert_not_called()
        client.starttls.assert_not_called()
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
