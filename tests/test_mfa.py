import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from test_auth_service import FakeAdminRepository, FakePermissionRepository  # noqa: E402  (sets the test env first)

from fastapi import HTTPException  # noqa: E402

from core import totp  # noqa: E402
from core.secret_box import seal, unseal  # noqa: E402
from schemas.auth_schema import (  # noqa: E402
    AdminCreateRequest,
    AdminUpdateRequest,
    LoginRequest,
    MfaVerifyRequest,
    PasskeyRegisterRequest,
)
from services.auth_service import AuthService  # noqa: E402
from soft_authenticator import SoftAuthenticator  # noqa: E402

ORIGIN = "http://localhost"
PASSWORD = "correct-horse-battery"


class TotpTests(unittest.TestCase):
    def test_rfc_6238_reference_values(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(totp.code_for(secret, 59 // 30), "287082")
        self.assertEqual(totp.code_for(secret, 1111111109 // 30), "081804")
        self.assertEqual(totp.code_for(secret, 2000000000 // 30), "279037")

    def test_window_and_replay(self):
        secret = totp.new_secret()
        now = time.time()
        current = totp.step_at(now)
        self.assertEqual(totp.match(secret, totp.code_for(secret, current), now), current)
        self.assertEqual(totp.match(secret, totp.code_for(secret, current - 1), now), current - 1)
        self.assertIsNone(totp.match(secret, totp.code_for(secret, current - 3), now))
        self.assertIsNone(totp.match(secret, totp.code_for(secret, current), now, last_step=current))
        self.assertIsNone(totp.match(secret, "12345", now))

    def test_provisioning_uri_is_scannable(self):
        uri = totp.provisioning_uri("ABCDEF", "ada@example.com", "Carrom Premier League")
        self.assertTrue(uri.startswith("otpauth://totp/Carrom%20Premier%20League%3Aada%40example.com?"))
        self.assertIn("secret=ABCDEF", uri)
        self.assertIn("issuer=Carrom+Premier+League", uri)

    def test_sealed_secrets_round_trip(self):
        sealed = seal("ABCDEF")
        self.assertNotIn("ABCDEF", sealed)
        self.assertEqual(unseal(sealed), "ABCDEF")


class MfaFlowTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeAdminRepository()
        self.service = AuthService(self.repository, FakePermissionRepository(self.repository))
        self.admin = self.service.bootstrap_admin(AdminCreateRequest(username="primary-admin", password=PASSWORD))
        self.service.request_email_code(self.admin.id, "owner@example.com", self.admin)
        self.admin = self.service.update_admin(
            self.admin.id,
            AdminUpdateRequest(email="owner@example.com", email_code=self._code(), phone_code="+91", phone_number="9000000077"),
            self.admin,
        )

    def _code(self):
        return self.service.mailer.sent[-1].text.split("Code: ", 1)[1].splitlines()[0]

    def _login(self, device="device-one"):
        return self.service.login(LoginRequest(username="primary-admin", password=PASSWORD, device_id=device, device_label="Chrome"))

    def test_without_factors_login_returns_a_token(self):
        token = self._login()
        self.assertFalse(token.mfa_required)
        self.assertTrue(token.access_token)
        self.assertFalse(self.service.mfa_status(self.admin).totp_enabled)

    def test_email_code_factor(self):
        status = self.service.set_email_factor(self.admin, True)
        self.assertTrue(status.email_enabled)
        challenge = self._login()
        self.assertTrue(challenge.mfa_required)
        self.assertEqual(challenge.access_token, "")
        self.assertEqual(challenge.mfa_methods, ["email"])
        self.service.send_login_code(challenge.mfa_token)
        mail = self.service.mailer.sent[-1]
        self.assertEqual(mail.to, "owner@example.com")
        self.assertIn("finish signing in", mail.text.lower())
        with self.assertRaises(HTTPException):
            self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="totp", code="123456"), ORIGIN)
        token = self.service.verify_mfa(
            MfaVerifyRequest(mfa_token=challenge.mfa_token, method="email", code=self._code()), ORIGIN
        )
        self.assertTrue(token.access_token)
        self.assertEqual(self.service.current_admin(token.access_token).id, self.admin.id)
        with self.assertRaises(HTTPException) as reused:
            self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="email", code="123456"), ORIGIN)
        self.assertEqual(reused.exception.status_code, 410)

    def test_authenticator_app_factor(self):
        setup = self.service.start_totp(self.admin)
        self.assertIn("otpauth://totp/", setup.otpauth_uri)
        self.assertFalse(self.service.mfa_status(self.admin).totp_enabled)
        current = totp.code_for(setup.secret, totp.step_at())
        with self.assertRaises(HTTPException):
            self.service.confirm_totp(self.admin, "111111" if current == "000000" else "000000")
        status = self.service.confirm_totp(self.admin, totp.code_for(setup.secret, totp.step_at()))
        self.assertTrue(status.totp_enabled)
        challenge = self._login()
        self.assertEqual(challenge.mfa_methods, ["totp"])
        replay = totp.code_for(setup.secret, totp.step_at())
        with self.assertRaises(HTTPException):
            self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="totp", code=replay), ORIGIN)
        next_code = totp.code_for(setup.secret, totp.step_at() + 1)
        token = self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="totp", code=next_code), ORIGIN)
        self.assertTrue(token.access_token)
        self.service.remove_totp(self.admin)
        self.assertFalse(self._login("device-one").mfa_required)

    def test_passkey_factor(self):
        authenticator = SoftAuthenticator()
        setup = self.service.passkey_registration_options(self.admin, ORIGIN)
        self.assertEqual(setup.options["rp"]["id"], "localhost")
        self.assertEqual(setup.options["user"]["name"], "primary-admin")
        status = self.service.register_passkey(
            self.admin,
            PasskeyRegisterRequest(challenge_id=setup.challenge_id, credential=authenticator.register(setup.options), label="Laptop"),
            ORIGIN,
        )
        self.assertEqual([key.label for key in status.passkeys], ["Laptop"])
        with self.assertRaises(HTTPException):
            self.service.register_passkey(
                self.admin,
                PasskeyRegisterRequest(challenge_id=setup.challenge_id, credential=authenticator.register(setup.options), label="Again"),
                ORIGIN,
            )

        challenge = self._login()
        self.assertEqual(challenge.mfa_methods, ["passkey"])
        options = self.service.passkey_login_options(challenge.mfa_token, ORIGIN)
        self.assertEqual(options["rpId"], "localhost")
        forged = authenticator.assert_login(options, challenge="b3RoZXItY2hhbGxlbmdl")
        with self.assertRaises(HTTPException):
            self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="passkey", credential=forged), ORIGIN)
        token = self.service.verify_mfa(
            MfaVerifyRequest(mfa_token=challenge.mfa_token, method="passkey", credential=authenticator.assert_login(options)),
            ORIGIN,
        )
        self.assertTrue(token.access_token)
        self.assertIsNotNone(self.service.mfa_status(self.admin).passkeys[0].last_used_at)

        with self.assertRaises(HTTPException) as wrong_site:
            self.service.passkey_registration_options(self.admin, "https://evil.example")
        self.assertEqual(wrong_site.exception.status_code, 400)
        removed = self.service.remove_passkey(self.admin, status.passkeys[0].id)
        self.assertEqual(removed.passkeys, [])

    def test_every_factor_is_offered_when_enabled(self):
        self.service.set_email_factor(self.admin, True)
        setup = self.service.start_totp(self.admin)
        self.service.confirm_totp(self.admin, totp.code_for(setup.secret, totp.step_at()))
        authenticator = SoftAuthenticator()
        options = self.service.passkey_registration_options(self.admin, ORIGIN)
        self.service.register_passkey(
            self.admin,
            PasskeyRegisterRequest(challenge_id=options.challenge_id, credential=authenticator.register(options.options), label="Key"),
            ORIGIN,
        )
        self.assertEqual(self._login().mfa_methods, ["passkey", "totp", "email"])

    def test_too_many_wrong_codes_end_the_sign_in(self):
        self.service.set_email_factor(self.admin, True)
        challenge = self._login()
        self.service.send_login_code(challenge.mfa_token)
        wrong = "111111" if self._code() == "000000" else "000000"
        for _ in range(4):
            with self.assertRaises(HTTPException) as raised:
                self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="email", code=wrong), ORIGIN)
            self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(HTTPException) as last:
            self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="email", code=wrong), ORIGIN)
        self.assertEqual(last.exception.status_code, 410)

    def test_second_device_still_needs_approval_after_mfa(self):
        self._login("device-one")
        self.service.set_email_factor(self.admin, True)
        challenge = self._login("device-two")
        self.service.send_login_code(challenge.mfa_token)
        result = self.service.verify_mfa(MfaVerifyRequest(mfa_token=challenge.mfa_token, method="email", code=self._code()), ORIGIN)
        self.assertTrue(result.pending)
        self.assertTrue(result.request_id)

    def test_a_new_account_activates_before_any_factor(self):
        created = self.service.create_admin(
            AdminCreateRequest(username="new-admin", email="new@example.com", phone_code="+91", phone_number="9000000099"),
            self.admin,
        )
        password = self.service.mailer.sent[-1].text.split("Temporary password: ", 1)[1].splitlines()[0]
        token = self.service.login(LoginRequest(username=created.username, password=password))
        self.assertFalse(token.mfa_required)
        self.assertTrue(token.must_change_password)
        self.assertFalse(self.service.get_admin(created.id).is_active)

    def test_email_factor_needs_an_email(self):
        bare = self.admin.model_copy(update={"email": None})
        with self.assertRaises(HTTPException):
            self.service.set_email_factor(bare, True)


if __name__ == "__main__":
    unittest.main()
