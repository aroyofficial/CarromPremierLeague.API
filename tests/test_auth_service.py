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

from datetime import date

from fastapi import HTTPException
from pydantic import ValidationError

from core.security import PASSWORD_ITERATIONS, decode_access_token, hash_password
from schemas.auth_schema import AdminCreateRequest, AdminUpdateRequest, LoginRequest
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
        )

    def get_all(self):
        return [self.get_by_id(item["Id"]) for item in self.records.values() if not item["Void"]]

    def create(self, username, password_hash, password_salt, password_iterations, profile=None):
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
            "IsActive": True,
            "Void": False,
            "FirstName": profile.get("first_name"),
            "LastName": profile.get("last_name"),
            "DateOfBirth": profile.get("date_of_birth"),
            "Gender": profile.get("gender"),
            "PhoneCode": profile.get("phone_code"),
            "PhoneNumber": profile.get("phone_number"),
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
                "first_name": "FirstName",
                "last_name": "LastName",
                "date_of_birth": "DateOfBirth",
                "gender": "Gender",
                "phone_code": "PhoneCode",
                "phone_number": "PhoneNumber",
                "email": "Email",
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

    def test_password_change_revokes_existing_tokens(self):
        token = self.service.login(
            LoginRequest(username="primary-admin", password="correct-horse-battery")
        )
        self.service.update_admin(
            self.primary.id,
            AdminUpdateRequest(password="new-correct-horse-battery"),
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

        cleared = self.service.update_admin(
            created.id,
            AdminUpdateRequest(last_name="King", phone_code=None, phone_number=None, email=None),
            self.primary,
        )
        self.assertEqual(cleared.last_name, "King")
        self.assertEqual(cleared.first_name, "Ada")
        self.assertIsNone(cleared.phone_number)
        self.assertIsNone(cleared.email)

    def test_profile_rejects_a_partial_phone_and_a_future_birthday(self):
        with self.assertRaises(ValidationError):
            AdminCreateRequest(username="ada-admin", password="another-password-123", phone_number="7700900123")
        with self.assertRaises(ValidationError):
            AdminCreateRequest(username="ada-admin", password="another-password-123", date_of_birth=date(2999, 1, 1))

    def test_second_bootstrap_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.bootstrap_admin(
                AdminCreateRequest(username="another-admin", password="another-password-123")
            )
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
