import os
import unittest
from datetime import timedelta

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

from core.security import PASSWORD_ITERATIONS, hash_password
from schemas.auth_schema import AdminUpdateRequest
from services.admin_codes import (
    CODE_EXPIRED,
    CODE_INCORRECT,
    CODE_LIMITED,
    CODE_MISSING,
    CODE_OK,
    CODE_USED,
    MAX_ATTEMPTS,
    MemoryCodeStore,
)
from services.auth_service import CODE_MESSAGES, AuthService
from tests.test_auth_service import FakeAdminRepository, FakePermissionRepository

NEW_PASSWORD = "a-brand-new-passphrase"
EMAIL = "new.admin@example.test"


class CodeStateTests(unittest.TestCase):
    """The activation screen's distinct failure states, checked through the real service path."""

    def setUp(self):
        self.repository = FakeAdminRepository()
        self.codes = MemoryCodeStore()
        self.service = AuthService(self.repository, FakePermissionRepository(self.repository), codes=self.codes)
        password_hash, password_salt = hash_password("temporary-password-1")
        self.admin_id = self.repository.create(
            "new-admin",
            password_hash,
            password_salt,
            PASSWORD_ITERATIONS,
            profile={"email": EMAIL},
            is_active=False,
            must_change_password=True,
        ).id
        self.current = self.repository.get_by_id(self.admin_id)

    def _activate(self, code):
        return self.service.update_admin(
            self.admin_id,
            AdminUpdateRequest(password=NEW_PASSWORD, password_code=code),
            self.current,
        )

    def _issue(self):
        return self.codes.issue(self.admin_id, "activation", EMAIL)

    def _expect(self, code, status, state):
        with self.assertRaises(HTTPException) as ctx:
            self._activate(code)
        self.assertEqual(ctx.exception.status_code, status)
        self.assertEqual(ctx.exception.detail, CODE_MESSAGES[state])

    def test_the_states_have_different_messages(self):
        self.assertEqual(len(set(CODE_MESSAGES.values())), len(CODE_MESSAGES))

    def test_no_code_sent_yet_is_missing(self):
        self._expect("123456", 400, CODE_MISSING)

    def test_a_wrong_code_is_incorrect_and_the_right_one_still_works(self):
        correct = self._issue()
        wrong = "000000" if correct != "000000" else "111111"
        self._expect(wrong, 400, CODE_INCORRECT)
        self._activate(correct)
        self.assertTrue(self.repository.get_by_id(self.admin_id).is_active)

    def test_an_expired_code_is_expired(self):
        code = self._issue()
        self.codes.rows[-1].expires_at -= timedelta(minutes=30)
        self._expect(code, 400, CODE_EXPIRED)

    def test_a_used_code_is_used(self):
        code = self._issue()
        self.codes.check(self.admin_id, "activation", EMAIL, code)  # first use consumes it
        self._expect(code, 400, CODE_USED)

    def test_too_many_wrong_codes_are_rate_limited(self):
        correct = self._issue()
        wrong = "000000" if correct != "000000" else "111111"
        for _ in range(MAX_ATTEMPTS - 1):
            self._expect(wrong, 400, CODE_INCORRECT)
        self._expect(wrong, 429, CODE_LIMITED)
        self._expect(correct, 429, CODE_LIMITED)

    def test_a_new_code_clears_the_limit(self):
        correct = self._issue()
        wrong = "000000" if correct != "000000" else "111111"
        for _ in range(MAX_ATTEMPTS):
            self.codes.check(self.admin_id, "activation", EMAIL, wrong)
        fresh = self._issue()
        self._activate(fresh)
        self.assertTrue(self.repository.get_by_id(self.admin_id).is_active)

    def test_a_code_sent_to_another_address_is_missing(self):
        code = self.codes.issue(self.admin_id, "activation", "someone.else@example.test")
        self._expect(code, 400, CODE_MISSING)

    def test_check_reports_ok_only_on_the_correct_code(self):
        code = self.codes.issue(self.admin_id, "activation", EMAIL)
        self.assertEqual(self.codes.check(self.admin_id, "activation", EMAIL, "999999" if code != "999999" else "888888"), CODE_INCORRECT)
        self.assertEqual(self.codes.check(self.admin_id, "activation", EMAIL, code), CODE_OK)


if __name__ == "__main__":
    unittest.main()
