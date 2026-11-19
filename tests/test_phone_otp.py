import os
import unittest
from datetime import timedelta
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

from fastapi import HTTPException

from core.config import settings
from core.security import PASSWORD_ITERATIONS, hash_password
from schemas.auth_schema import AdminUpdateRequest
from services import phone_otp
from services.auth_service import AuthService
from services.phone_otp import (
    COOLDOWN_SECONDS,
    MAX_ATTEMPTS,
    MAX_PER_HOUR,
    DevPhoneSender,
    DisabledPhoneSender,
    MemoryPhoneOtpStore,
    PhoneOtpService,
    sender_from_settings,
)
from tests.test_auth_service import FakeAdminRepository, FakePermissionRepository

PHONE = "9876543210"
E164 = "+919876543210"


def _make_admin(repository, username, email):
    password_hash, password_salt = hash_password("correct-horse-battery")
    return repository.create(
        username,
        password_hash,
        password_salt,
        PASSWORD_ITERATIONS,
        profile={"email": email},
    )


class PhoneOtpTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeAdminRepository()
        self.sender = DevPhoneSender()
        self.store = MemoryPhoneOtpStore()
        self.otp = PhoneOtpService(self.store, self.sender)
        self.service = AuthService(
            self.repository,
            FakePermissionRepository(self.repository),
            phone_otp=self.otp,
        )
        self.admin = _make_admin(self.repository, "phone-admin", "phone@example.test")
        self.current = self.repository.get_by_id(self.admin.id)

    def _request(self, admin=None):
        admin = admin or self.current
        self.service.request_phone_code(admin.id, "+91", PHONE, admin)

    def _code(self):
        return self.sender.last_code[E164]

    def _backdate_all(self, minutes):
        for row in self.store.rows:
            row["at"] -= timedelta(minutes=minutes)
            row["expires_at"] -= timedelta(minutes=minutes)

    def test_default_sender_is_off_and_refuses_requests(self):
        with mock.patch.object(settings, "PHONE_OTP_PROVIDER", "off"):
            self.assertFalse(sender_from_settings().configured)
        disabled = AuthService(
            self.repository,
            FakePermissionRepository(self.repository),
            phone_otp=PhoneOtpService(MemoryPhoneOtpStore(), DisabledPhoneSender()),
        )
        with self.assertRaises(HTTPException) as ctx:
            disabled.request_phone_code(self.admin.id, "+91", PHONE, self.current)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertNotIn(E164, str(ctx.exception.detail))
        self.assertFalse(disabled.get_admin(self.admin.id).phone_verification_enabled)
        self.assertTrue(self.service.get_admin(self.admin.id).phone_verification_enabled)

    def test_dev_sender_is_never_used_outside_dev(self):
        with mock.patch.object(settings, "PHONE_OTP_PROVIDER", "dev"), mock.patch.object(settings, "APP_ENV", "prod"):
            self.assertFalse(sender_from_settings().configured)
        with mock.patch.object(settings, "PHONE_OTP_PROVIDER", "dev"), mock.patch.object(settings, "APP_ENV", "dev"):
            self.assertTrue(sender_from_settings().configured)

    def test_probing_a_number_someone_else_holds_is_rate_limited(self):
        other = _make_admin(self.repository, "other-admin", "other@example.test")
        self.repository.records[other.id]["PhoneE164"] = E164
        with self.assertRaises(HTTPException) as ctx:
            self._request()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.sender.last_code, {})
        with self.assertRaises(HTTPException) as ctx:
            self._request()
        self.assertEqual(ctx.exception.status_code, 429)

    def test_the_dev_sender_is_shared_so_the_code_survives_between_requests(self):
        with mock.patch.object(settings, "PHONE_OTP_PROVIDER", "dev"), mock.patch.object(settings, "APP_ENV", "dev"):
            self.assertIs(sender_from_settings(), sender_from_settings())

    def test_only_indian_numbers_are_accepted(self):
        with self.assertRaises(HTTPException) as ctx:
            self.service.request_phone_code(self.admin.id, "+1", "2025550123", self.current)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_correct_code_marks_the_phone_verified(self):
        self._request()
        self.assertEqual(len(self.sender.last_code), 1)
        updated = self.service.confirm_phone_code(self.admin.id, "+91", PHONE, self._code(), self.current)
        self.assertTrue(updated.phone_verified)
        self.assertEqual(updated.phone_code, "+91")
        self.assertEqual(updated.phone_number, PHONE)

    def test_code_is_stored_hashed_never_plaintext(self):
        self._request()
        code = self._code()
        for row in self.store.rows:
            self.assertNotEqual(row["hash"], code)
            self.assertNotIn(code, row["hash"])
            self.assertNotIn(code, str(row["salt"]))

    def test_unverified_phone_is_not_trusted(self):
        self.service.update_admin(
            self.admin.id,
            AdminUpdateRequest(phone_code="+91", phone_number=PHONE),
            self.current,
        )
        self.assertFalse(self.repository.get_by_id(self.admin.id).phone_verified)

    def test_wrong_codes_are_limited_and_then_locked(self):
        self._request()
        correct = self._code()
        wrong = "000000" if correct != "000000" else "111111"
        for _ in range(MAX_ATTEMPTS - 1):
            with self.assertRaises(HTTPException) as ctx:
                self.service.confirm_phone_code(self.admin.id, "+91", PHONE, wrong, self.current)
            self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            self.service.confirm_phone_code(self.admin.id, "+91", PHONE, wrong, self.current)
        self.assertEqual(ctx.exception.status_code, 429)
        with self.assertRaises(HTTPException) as ctx:
            self.service.confirm_phone_code(self.admin.id, "+91", PHONE, correct, self.current)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertFalse(self.repository.get_by_id(self.admin.id).phone_verified)

    def test_expired_code_is_rejected(self):
        self._request()
        self._backdate_all(minutes=11)
        with self.assertRaises(HTTPException) as ctx:
            self.service.confirm_phone_code(self.admin.id, "+91", PHONE, self._code(), self.current)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("expired", ctx.exception.detail)

    def test_used_code_cannot_be_replayed(self):
        self._request()
        code = self._code()
        self.service.confirm_phone_code(self.admin.id, "+91", PHONE, code, self.current)
        with self.assertRaises(HTTPException) as ctx:
            self.service.confirm_phone_code(self.admin.id, "+91", PHONE, code, self.current)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("already used", ctx.exception.detail)

    def test_resend_cooldown_is_enforced(self):
        self._request()
        with self.assertRaises(HTTPException) as ctx:
            self._request()
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers.get("Retry-After"), str(COOLDOWN_SECONDS))

    def test_hourly_send_cap_is_enforced(self):
        for _ in range(MAX_PER_HOUR):
            self._request()
            self._backdate_all(minutes=2)
        with self.assertRaises(HTTPException) as ctx:
            self._request()
        self.assertEqual(ctx.exception.status_code, 429)

    def test_another_admin_cannot_use_the_code(self):
        self._request()
        code = self._code()
        other = _make_admin(self.repository, "other-admin", "other@example.test")
        other_current = self.repository.get_by_id(other.id)
        with self.assertRaises(HTTPException) as ctx:
            self.service.confirm_phone_code(other.id, "+91", PHONE, code, other_current)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(self.repository.get_by_id(self.admin.id).phone_verified)

    def test_a_phone_owned_by_someone_else_cannot_be_verified(self):
        self.repository.records[self.admin.id]["PhoneE164"] = E164
        other = _make_admin(self.repository, "other-admin", "other@example.test")
        other_current = self.repository.get_by_id(other.id)
        with self.assertRaises(HTTPException) as ctx:
            self.service.request_phone_code(other.id, "+91", PHONE, other_current)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.sender.last_code, {})

    def test_admin_cannot_request_codes_for_another_admin(self):
        other = _make_admin(self.repository, "other-admin", "other@example.test")
        with self.assertRaises(HTTPException) as ctx:
            self.service.request_phone_code(other.id, "+91", PHONE, self.current)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_changing_a_verified_phone_clears_its_verification(self):
        self._request()
        self.service.confirm_phone_code(self.admin.id, "+91", PHONE, self._code(), self.current)
        self.assertTrue(self.repository.get_by_id(self.admin.id).phone_verified)
        self.service.update_admin(
            self.admin.id,
            AdminUpdateRequest(phone_code="+91", phone_number="9123456789"),
            self.current,
        )
        self.assertFalse(self.repository.get_by_id(self.admin.id).phone_verified)

    def test_clearing_the_phone_clears_its_verification(self):
        self._request()
        self.service.confirm_phone_code(self.admin.id, "+91", PHONE, self._code(), self.current)
        self.service.update_admin(
            self.admin.id,
            AdminUpdateRequest(phone_code=None, phone_number=None),
            self.current,
        )
        self.assertFalse(self.repository.get_by_id(self.admin.id).phone_verified)


if __name__ == "__main__":
    unittest.main()
