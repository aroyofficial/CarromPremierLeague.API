import os
import unittest

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

from datetime import date, datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError

from core.security import PASSWORD_ITERATIONS, decode_access_token, hash_password
from schemas.auth_schema import (
    AdminCreateRequest,
    AdminUpdateRequest,
    ChangePasswordRequest,
    LoginRequest,
    ResetCodeRequest,
    ResetPasswordRequest,
)
from services.auth_service import AuthService


class FakeAdminRepository:
    def __init__(self):
        self.records = {}
        self.next_id = 1

    def count_active(self):
        return sum(1 for item in self.records.values() if not item["Void"] and item["IsActive"])

    def count_all(self, lock=False):
        return len(self.records)

    def get_credentials_by_username(self, username):
        for item in self.records.values():
            if item["Username"] == username and not item["Void"]:
                return item.copy()
        return None

    def get_by_id(self, admin_id):
        item = self.records.get(admin_id)
        if not item or item["Void"]:
            return None
        from schemas.auth_schema import AdminResponse
        return AdminResponse(
            id=item["Id"],
            username=item["Username"],
            is_active=item["IsActive"],
            first_name=item.get("FirstName"),
            last_name=item.get("LastName"),
            date_of_birth=item.get("DateOfBirth"),
            gender=item.get("Gender"),
            phone_code=item.get("PhoneCode"),
            phone_number=item.get("PhoneNumber"),
            email=item.get("Email"),
            avatar_url=item.get("AvatarUrl"),
            must_change_password=bool(item.get("MustChangePassword")),
            password_changed_at=item.get("PasswordChangedAt"),
            phone_verified=item.get("PhoneVerifiedAt") is not None,
        )

    def get_all(self):
        return [self.get_by_id(item["Id"]) for item in self.records.values() if not item["Void"]]

    def find_email(self, email):
        for item in self.records.values():
            if not item["Void"] and item.get("Email") == email:
                return item["Id"]
        return None

    def find_phone(self, e164):
        for item in self.records.values():
            if not item["Void"] and item.get("PhoneE164") == e164:
                return ("admin", item["Id"])
        return None

    def create(self, username, password_hash, password_salt, password_iterations, profile=None, is_active=True, must_change_password=False):
        admin_id = self.next_id
        self.next_id += 1
        profile = profile or {}
        self.records[admin_id] = {
            "Id": admin_id,
            "Username": username,
            "PasswordHash": password_hash,
            "PasswordSalt": password_salt,
            "PasswordIterations": password_iterations,
            "TokenVersion": 1,
            "IsActive": is_active,
            "MustChangePassword": must_change_password,
            "Void": False,
            "FirstName": profile.get("first_name"),
            "LastName": profile.get("last_name"),
            "DateOfBirth": profile.get("date_of_birth"),
            "Gender": profile.get("gender"),
            "PhoneCode": profile.get("phone_code"),
            "PhoneNumber": profile.get("phone_number"),
            "PhoneE164": profile.get("phone_e164"),
            "Email": profile.get("email"),
            "AvatarUrl": None,
        }
        return self.get_by_id(admin_id)

    def update(self, admin_id, values):
        item = self.records[admin_id]
        if values.pop("increment_token_version", False):
            item["TokenVersion"] += 1
        for key, value in values.items():
            item[{
                "username": "Username",
                "password_hash": "PasswordHash",
                "password_salt": "PasswordSalt",
                "password_iterations": "PasswordIterations",
                "is_active": "IsActive",
                "must_change_password": "MustChangePassword",
                "first_name": "FirstName",
                "last_name": "LastName",
                "date_of_birth": "DateOfBirth",
                "gender": "Gender",
                "phone_code": "PhoneCode",
                "phone_number": "PhoneNumber",
                "phone_e164": "PhoneE164",
                "phone_verified_at": "PhoneVerifiedAt",
                "email": "Email",
                "password_changed_at": "PasswordChangedAt",
            }[key]] = value
        return self.get_by_id(admin_id)

    def delete(self, admin_id):
        self.records[admin_id]["Void"] = True
        self.records[admin_id]["IsActive"] = False
        return True


class FakePermissionRepository:
    def __init__(self, admins: FakeAdminRepository):
        self.admins = admins
        self.grants: dict[int, set[str]] = {}

    def codes_for(self, admin_id):
        from core.permissions import ordered
        return ordered(self.grants.get(admin_id, set()))

    def codes_by_admin(self):
        return {admin_id: self.codes_for(admin_id) for admin_id in self.grants}

    def active_holder_ids(self, code, lock=False):
        return {
            admin_id
            for admin_id, codes in self.grants.items()
            if code in codes
            and not self.admins.records[admin_id]["Void"]
            and self.admins.records[admin_id]["IsActive"]
        }

    def change(self, admin_id, grant, revoke, granted_by):
        codes = self.grants.setdefault(admin_id, set())
        codes.difference_update(revoke)
        codes.update(grant)


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeAdminRepository()
        self.service = AuthService(self.repository, FakePermissionRepository(self.repository))
        self.primary = self.service.bootstrap_admin(
            AdminCreateRequest(username="primary-admin", password="correct-horse-battery")
        )

    def test_live_mail_returns_before_the_provider_answers(self):
        import threading
        import time

        gate = threading.Event()

        class SlowMailer:
            defer = True
            configured = True

        self.service.mailer = SlowMailer()
        started = time.perf_counter()
        self.service._send(lambda: gate.wait(2), None)
        self.assertLess(time.perf_counter() - started, 0.5)
        gate.set()

    def test_login_returns_valid_bearer_token(self):
        token = self.service.login(
            LoginRequest(username="PRIMARY-ADMIN", password="correct-horse-battery")
        )
        payload = decode_access_token(token.access_token)

        self.assertEqual(token.token_type, "bearer")
        self.assertEqual(payload["sub"], str(self.primary.id))
        self.assertEqual(payload["username"], "primary-admin")
        self.assertEqual(self.service.current_admin(token.access_token).id, self.primary.id)

    def test_bad_password_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.login(LoginRequest(username="primary-admin", password="wrong-password-123"))
        self.assertEqual(raised.exception.status_code, 401)

    def _code(self):
        return self.service.mailer.sent[-1].text.split("Code: ", 1)[1].splitlines()[0]

    def _give_primary_email(self, email="owner@example.com"):
        self.service.request_email_code(self.primary.id, email, self.primary)
        self.primary = self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(email=email, email_code=self._code(), phone_code="+91", phone_number="9000000077"),
            self.primary,
        )

    def _create(self, username, email, phone="9000000099", **profile):
        return self.service.create_admin(
            AdminCreateRequest(username=username, email=email, phone_code="+91", phone_number=phone, **profile),
            self.primary,
        )

    def _temporary_password(self):
        return self.service.mailer.sent[-1].text.split("Temporary password: ", 1)[1].splitlines()[0]

    def _activate(self, created, password="replacement-password"):
        token = self.service.login(LoginRequest(username=created.username, password=self._temporary_password()))
        current = self.service.current_admin(token.access_token)
        self.service.request_password_code(current)
        return self.service.update_admin(
            created.id,
            AdminUpdateRequest(password=password, password_code=self._code()),
            current,
        )

    def test_password_change_revokes_existing_tokens(self):
        self._give_primary_email()
        token = self.service.login(
            LoginRequest(username="primary-admin", password="correct-horse-battery")
        )
        self.service.request_password_code(self.primary)
        self.assertIn("confirm the new password", self.service.mailer.sent[-1].text.lower())
        self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(password="new-correct-horse-battery", password_code=self._code()),
            self.primary,
        )

        with self.assertRaises(HTTPException) as raised:
            self.service.current_admin(token.access_token)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIsNotNone(
            self.service.login(LoginRequest(username="primary-admin", password="new-correct-horse-battery"))
        )

    def test_last_active_admin_cannot_be_deactivated_or_deleted(self):
        with self.assertRaises(HTTPException):
            self.service.update_admin(
                self.primary.id,
                AdminUpdateRequest(is_active=False),
                self.primary,
            )
        with self.assertRaises(HTTPException):
            self.service.delete_admin(self.primary.id, self.primary)

    def test_seed_admin_credentials_satisfy_login_rules(self):
        from core.admin_seed import SEED_PASSWORD, SEED_USERNAME
        from core.security import verify_password

        request = AdminCreateRequest(username=SEED_USERNAME, password=SEED_PASSWORD)
        password_hash, password_salt = hash_password(request.password)

        self.assertEqual(request.username, "admin")
        self.assertTrue(verify_password(SEED_PASSWORD, password_hash, password_salt, PASSWORD_ITERATIONS))

    def test_profile_is_saved_and_can_be_cleared(self):
        created = self.service.create_admin(
            AdminCreateRequest(
                username="ada-admin",
                password="another-password-123",
                first_name=" Ada ",
                last_name="Lovelace",
                date_of_birth=date(1815, 12, 10),
                gender="Female",
                phone_code="+44",
                phone_number="7700900123",
                email="Ada@Example.com",
            ),
            self.primary,
        )
        self.assertEqual(created.first_name, "Ada")
        self.assertEqual(created.gender, "female")
        self.assertEqual(created.email, "ada@example.com")
        self.assertEqual(created.phone_code, "+44")
        self.assertEqual(created.date_of_birth, date(1815, 12, 10))

        self._give_primary_email()
        renamed = self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(last_name="King"),
            self.primary,
        )
        self.assertEqual(renamed.last_name, "King")
        self.assertEqual(renamed.email, "owner@example.com")
        with self.assertRaises(HTTPException):
            self.service.update_admin(self.primary.id, AdminUpdateRequest(email=None), self.primary)
        cleared = self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(phone_code=None, phone_number=None),
            self.primary,
        )
        self.assertIsNone(cleared.phone_number)

    def test_username_is_generated_and_cannot_change(self):
        created = self.service.create_admin(
            AdminCreateRequest(
                first_name="Ada",
                last_name="Lovelace",
                email="ada@example.com",
                phone_code="+91",
                phone_number="9000000081",
            ),
            self.primary,
        )
        self.assertEqual(created.username, "ada.lovelace")
        active = self._activate(created)
        second = self.service.create_admin(
            AdminCreateRequest(
                first_name="Ada",
                last_name="Lovelace",
                email="ada.other@example.com",
                phone_code="+91",
                phone_number="9000000082",
            ),
            self.primary,
        )
        self.assertEqual(second.username, "ada.lovelace.ada.other")
        with self.assertRaises(HTTPException) as raised:
            self.service.update_admin(active.id, AdminUpdateRequest(username="someone-else"), active)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Username cannot be changed.")
        kept = self.service.update_admin(active.id, AdminUpdateRequest(last_name="King"), active)
        self.assertEqual(kept.last_name, "King")
        self.assertEqual(kept.username, "ada.lovelace")

    def test_profile_rejects_a_partial_phone_and_a_future_birthday(self):
        with self.assertRaises(ValidationError):
            AdminCreateRequest(username="ada-admin", password="another-password-123", phone_number="7700900123")
        with self.assertRaises(ValidationError):
            AdminCreateRequest(username="ada-admin", password="another-password-123", date_of_birth=date(2999, 1, 1))

    def test_new_admin_stays_off_until_the_emailed_password_is_changed(self):
        created = self.service.create_admin(
            AdminCreateRequest(
                username="new-admin",
                email="new-admin@example.com",
                phone_code="+91",
                phone_number="9000000099",
                first_name="New",
            ),
            self.primary,
        )
        self.assertFalse(created.is_active)
        self.assertTrue(created.must_change_password)
        welcome = self.service.mailer.sent[-1]
        self.assertIn("default password", welcome.text.lower())
        self.assertIn("new-admin@example.com", welcome.html)
        self.assertIn("+91 9000000099", welcome.text)
        password = welcome.text.split("Temporary password: ", 1)[1].splitlines()[0]
        self.assertNotIn("another-password", password)
        token = self.service.login(LoginRequest(username=created.username, password=password))
        self.assertTrue(token.must_change_password)
        with self.assertRaises(HTTPException):
            self.service.login(LoginRequest(username=created.username, password="correct-horse-battery"))
        current = self.service.current_admin(token.access_token)
        with self.assertRaises(HTTPException) as no_code:
            self.service.update_admin(created.id, AdminUpdateRequest(password="replacement-password"), current)
        self.assertEqual(no_code.exception.detail, "Send a code first, then enter it here.")
        self.service.request_password_code(current)
        verify_mail = self.service.mailer.sent[-1]
        self.assertEqual(verify_mail.to, "new-admin@example.com")
        self.assertIn("verify this email address", verify_mail.text.lower())
        wrong = "111111" if self._code() == "000000" else "000000"
        with self.assertRaises(HTTPException):
            self.service.update_admin(
                created.id,
                AdminUpdateRequest(password="replacement-password", password_code=wrong),
                current,
            )
        self.service.request_password_code(current)
        activated = self.service.update_admin(
            created.id,
            AdminUpdateRequest(password="replacement-password", password_code=self._code()),
            current,
        )
        self.assertTrue(activated.is_active)
        self.assertFalse(activated.must_change_password)
        self.assertIn("now active", self.service.mailer.sent[-1].text.lower() + self.service.mailer.sent[-1].html.lower())
        signed_in = self.service.login(LoginRequest(username=created.username, password="replacement-password"))
        self.assertFalse(signed_in.must_change_password)

    def test_changing_an_email_needs_the_code_sent_to_it(self):
        self._give_primary_email("first@example.com")
        with self.assertRaises(HTTPException):
            self.service.update_admin(
                self.primary.id,
                AdminUpdateRequest(email="next@example.com"),
                self.primary,
            )
        self.service.request_email_code(self.primary.id, "next@example.com", self.primary)
        self.assertEqual(self.service.mailer.sent[-1].to, "next@example.com")
        updated = self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(email="next@example.com", email_code=self._code()),
            self.primary,
        )
        self.assertEqual(updated.email, "next@example.com")

    def test_create_needs_an_email_and_accepts_a_missing_phone(self):
        with self.assertRaises(HTTPException) as no_email:
            self.service.create_admin(
                AdminCreateRequest(username="no-mail", phone_code="+91", phone_number="9000000011"),
                self.primary,
            )
        self.assertEqual(no_email.exception.detail, "Email is required.")
        created = self.service.create_admin(
            AdminCreateRequest(username="no-phone", email="nophone@example.com"),
            self.primary,
        )
        self.assertIsNone(created.phone_number)
        for number in ("900000001", "90000000111", "90000-0001", "abcdefghij"):
            with self.assertRaises(ValidationError) as invalid:
                AdminCreateRequest(username="bad-phone", email="p@example.com", phone_code="+91", phone_number=number)
            self.assertIn("valid", str(invalid.exception).lower())
        self.assertEqual(
            AdminCreateRequest(username="ok-phone", email="p@example.com", phone_code="+91", phone_number=" 9000000011 ").phone_number,
            "9000000011",
        )
        self.assertEqual(
            AdminCreateRequest(username="trunk", email="t@example.com", phone_code="+91", phone_number="09000000011").phone_number,
            "9000000011",
        )

    def test_the_same_phone_cannot_be_saved_twice(self):
        self._create("first-phone", "first-phone@example.com", "9000000041")
        with self.assertRaises(HTTPException) as raised:
            self._create("second-phone", "second-phone@example.com", "09000 000041")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "That phone number is already used.")

    def test_no_admin_can_edit_another_admins_profile_or_password(self):
        self._give_primary_email()
        created = self._create("other-admin", "other@example.com")
        other = self._activate(created)
        for change in (
            AdminUpdateRequest(first_name="Changed"),
            AdminUpdateRequest(password="someone-elses-password"),
            AdminUpdateRequest(username="renamed-admin"),
            AdminUpdateRequest(email="else@example.com", email_code="123456"),
        ):
            with self.assertRaises(HTTPException) as raised:
                self.service.update_admin(other.id, change, self.primary)
            self.assertEqual(raised.exception.status_code, 403)
        with self.assertRaises(HTTPException) as code_for_other:
            self.service.request_email_code(other.id, "else@example.com", self.primary)
        self.assertEqual(code_for_other.exception.status_code, 403)
        other_session = self.service.current_admin(
            self.service.login(LoginRequest(username=other.username, password="replacement-password")).access_token
        )
        with self.assertRaises(HTTPException):
            self.service.update_admin(self.primary.id, AdminUpdateRequest(password="taken-over-password"), other_session)
        self.service.login(LoginRequest(username="primary-admin", password="correct-horse-battery"))

    def test_the_creator_cannot_edit_or_activate_a_new_account(self):
        created = self._create("fresh-admin", "fresh@example.com")
        with self.assertRaises(HTTPException) as activate:
            self.service.update_admin(created.id, AdminUpdateRequest(is_active=True), self.primary)
        self.assertIn("activates itself", activate.exception.detail)
        with self.assertRaises(HTTPException) as edit:
            self.service.update_admin(created.id, AdminUpdateRequest(last_name="Edited"), self.primary)
        self.assertEqual(edit.exception.status_code, 403)
        self.assertFalse(self.service.get_admin(created.id).is_active)

    def test_an_active_admin_can_be_switched_off_and_on_by_a_manager(self):
        self._give_primary_email()
        other = self._activate(self._create("toggle-admin", "toggle@example.com"))
        off = self.service.update_admin(other.id, AdminUpdateRequest(is_active=False), self.primary)
        self.assertFalse(off.is_active)
        on = self.service.update_admin(other.id, AdminUpdateRequest(is_active=True), self.primary)
        self.assertTrue(on.is_active)

    def test_password_change_needs_an_email_on_the_account(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.request_password_code(self.primary)
        self.assertIn("Add an email address", raised.exception.detail)
        with self.assertRaises(HTTPException):
            self.service.update_admin(
                self.primary.id,
                AdminUpdateRequest(password="new-correct-horse-battery", password_code="123456"),
                self.primary,
            )

    def test_password_reset_uses_the_username(self):
        self.service.request_password_reset("primary-admin")
        self.assertEqual(self.service.mailer.sent, [])
        self.service.request_email_code(self.primary.id, "owner@example.com", self.primary)
        code = self.service.mailer.sent[-1].text.split("Code: ", 1)[1].splitlines()[0]
        self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(email="owner@example.com", email_code=code, phone_code="+91", phone_number="9000000077"),
            self.primary,
        )
        self.service.request_password_reset("PRIMARY-ADMIN")
        reset_code = self.service.mailer.sent[-1].text.split("Code: ", 1)[1].splitlines()[0]
        self.service.reset_password(ResetPasswordRequest(
            username="primary-admin",
            code=reset_code,
            password="reset-password-123",
        ))
        self.service.login(LoginRequest(username="primary-admin", password="reset-password-123"))
        with self.assertRaises(HTTPException):
            self.service.login(LoginRequest(username="primary-admin", password="correct-horse-battery"))

    def test_profile_change_checks_the_code_before_the_password(self):
        self._give_primary_email()
        token = self.service.login(LoginRequest(username="primary-admin", password="correct-horse-battery"))
        self.service.request_password_code(self.primary)
        with self.assertRaises(HTTPException) as wrong:
            self.service.verify_password_code(self.primary, "000000" if self._code() != "000000" else "111111")
        self.assertEqual(wrong.exception.status_code, 400)
        self.service.request_password_code(self.primary)
        ticket = self.service.verify_password_code(self.primary, self._code())
        self.assertGreater(len(ticket.ticket), 20)
        self.assertEqual(ticket.expires_in, 600)

        self.service.change_password(self.primary, ChangePasswordRequest(ticket=ticket.ticket, password="brand-new-password-42"))

        with self.assertRaises(HTTPException) as signed_out:
            self.service.current_admin(token.access_token)
        self.assertEqual(signed_out.exception.status_code, 401)
        self.service.login(LoginRequest(username="primary-admin", password="brand-new-password-42"))
        self.assertIsNotNone(self.service.get_admin(self.primary.id).password_changed_at)
        follow_up = self.service.mailer.sent[-1]
        self.assertEqual(follow_up.subject, "Your admin password was changed")
        self.assertIn("When:", follow_up.text)
        self.assertIn("Changed from My profile", follow_up.text)

    def test_a_change_ticket_works_once(self):
        self._give_primary_email()
        self.service.request_password_code(self.primary)
        ticket = self.service.verify_password_code(self.primary, self._code()).ticket
        self.service.change_password(self.primary, ChangePasswordRequest(ticket=ticket, password="brand-new-password-42"))
        with self.assertRaises(HTTPException) as again:
            self.service.change_password(self.primary, ChangePasswordRequest(ticket=ticket, password="another-password-4242"))
        self.assertIn("expired", again.exception.detail)
        with self.assertRaises(HTTPException):
            self.service.change_password(self.primary, ChangePasswordRequest(ticket="x" * 43, password="another-password-4242"))

    def test_reset_checks_the_code_then_sets_the_password_with_a_ticket(self):
        self._give_primary_email()
        self.service.request_password_reset("primary-admin")
        with self.assertRaises(HTTPException):
            self.service.verify_reset_code(ResetCodeRequest(username="primary-admin", code="000000" if self._code() != "000000" else "111111"))
        self.service.request_password_reset("primary-admin")
        ticket = self.service.verify_reset_code(ResetCodeRequest(username="primary-admin", code=self._code()))
        self.service.reset_password(ResetPasswordRequest(username="primary-admin", ticket=ticket.ticket, password="reset-with-ticket-99"))
        self.service.login(LoginRequest(username="primary-admin", password="reset-with-ticket-99"))
        follow_up = self.service.mailer.sent[-1]
        self.assertIn("Reset with an email code", follow_up.text)
        self.assertIsNotNone(self.service.get_admin(self.primary.id).password_changed_at)
        with self.assertRaises(HTTPException):
            self.service.reset_password(ResetPasswordRequest(username="primary-admin", ticket=ticket.ticket, password="reset-with-ticket-00"))

    def test_reset_needs_a_code_or_a_ticket(self):
        with self.assertRaises(ValidationError):
            ResetPasswordRequest(username="primary-admin", password="reset-with-ticket-99")

    def test_unknown_username_reset_does_not_reveal_the_account(self):
        self.service.request_password_reset("missing-admin")
        self.assertEqual(self.service.mailer.sent, [])

    def test_the_same_device_can_sign_in_again(self):
        first = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-one", device_label="Chrome"
        ))
        again = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-one", device_label="Chrome"
        ))
        self.assertFalse(again.pending)
        self.assertEqual(self.service.current_admin(first.access_token).id, self.primary.id)
        self.assertEqual(self.service.current_admin(again.access_token).id, self.primary.id)

    def test_another_device_waits_until_the_current_session_approves(self):
        first = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-one", device_label="Chrome on Windows"
        ))
        second = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-two", device_label="Firefox on Windows"
        ))
        self.assertTrue(second.pending)
        self.assertEqual(second.access_token, "")
        self.assertEqual(self.service.current_admin(first.access_token).id, self.primary.id)
        notice = self.service.pending_login(self.primary)
        self.assertEqual(notice.device_label, "Firefox on Windows")
        remaining = (notice.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
        self.assertGreater(remaining, 590)
        self.assertLessEqual(remaining, 600)
        waiting = self.service.claim_login(second.request_id)
        self.assertTrue(waiting.pending)
        self.service.approve_login(self.primary, notice.id)
        claimed = self.service.claim_login(second.request_id)
        self.assertTrue(claimed.access_token)
        with self.assertRaises(HTTPException):
            self.service.current_admin(first.access_token)
        self.assertEqual(self.service.current_admin(claimed.access_token).id, self.primary.id)

    def test_declining_a_sign_in_keeps_the_current_session(self):
        first = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-one", device_label="Chrome"
        ))
        second = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-two", device_label="Firefox"
        ))
        self.service.deny_login(self.primary, second.request_id)
        with self.assertRaises(HTTPException) as raised:
            self.service.claim_login(second.request_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.service.current_admin(first.access_token).id, self.primary.id)

    def test_the_old_device_cannot_clear_the_approved_session(self):
        self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-one", device_label="Chrome"
        ))
        second = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-two", device_label="Firefox"
        ))
        self.service.approve_login(self.primary, second.request_id)
        self.service.logout(self.primary, "device-one")
        third = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-three", device_label="Edge"
        ))
        self.assertTrue(third.pending)

    def test_logout_lets_another_device_sign_in(self):
        self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-one", device_label="Chrome"
        ))
        self.service.logout(self.primary)
        other = self.service.login(LoginRequest(
            username="primary-admin", password="correct-horse-battery", device_id="device-two", device_label="Firefox"
        ))
        self.assertFalse(other.pending)
        self.assertTrue(other.access_token)

    def test_second_bootstrap_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.bootstrap_admin(
                AdminCreateRequest(username="another-admin", password="another-password-123")
            )
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
