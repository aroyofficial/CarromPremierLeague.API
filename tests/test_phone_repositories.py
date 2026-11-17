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

from datetime import timedelta

from fastapi import HTTPException

from core.phone_numbers import PhoneTaken
from repositories.admin_repository import AdminRepository
from repositories.player_repository import PlayerRepository
from schemas.player_schema import PlayerCreateRequest, PlayerUpdateRequest
from services.phone_otp import MemoryPhoneOtpStore, PhoneOtpService, _hash, _utcnow

PHONE_INDEX_ERROR = (
    "('23000', \"[23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Cannot insert duplicate "
    "key row in object 'dbo.tblPlayers' with unique index 'UX_tblPlayers_PhoneE164'. The duplicate key "
    "value is (+919876543210). (2601)\")"
)
USERNAME_ERROR = (
    "Violation of UNIQUE KEY constraint 'UQ_tblAdmins_Username'. Cannot insert duplicate key in object "
    "'dbo.tblAdmins'. The duplicate key value is (asha). (2627)"
)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 1
        self.lastrowid = 7

    def execute(self, sql, params=()):
        self.db.statements.append(" ".join(sql.split()))
        for fragment, error in self.db.fail_on.items():
            if fragment in sql:
                raise Exception(error)

    def fetchone(self):
        # Like core.database: SCOPE_IDENTITY() in a later batch is NULL, so the id must come from lastrowid.
        return {"Id": None}


class FakeDb:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on or {}
        self.statements = []
        self.rolled_back = False
        self.committed = False

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


PHONE = {"phone_code": "+91", "phone_number": "9876543210", "phone_e164": "+919876543210"}


class ProfileUniqueIndexTests(unittest.TestCase):
    def test_a_player_insert_that_loses_the_race_becomes_phone_taken(self):
        db = FakeDb({"INSERT INTO tblPlayers": PHONE_INDEX_ERROR})
        with self.assertRaises(PhoneTaken):
            PlayerRepository(db).create(PlayerCreateRequest(first_name="Asha", last_name="Rao"), PHONE)
        self.assertTrue(db.rolled_back)
        self.assertFalse(db.committed)

    def test_a_player_update_that_loses_the_race_becomes_phone_taken(self):
        db = FakeDb({"UPDATE tblPlayers": PHONE_INDEX_ERROR})
        with self.assertRaises(PhoneTaken):
            PlayerRepository(db).update(7, PlayerUpdateRequest(), PHONE)
        self.assertTrue(db.rolled_back)

    def test_an_admin_insert_that_loses_the_race_becomes_phone_taken(self):
        db = FakeDb({"INSERT INTO tblAdmins": PHONE_INDEX_ERROR.replace("tblPlayers", "tblAdmins")})
        with self.assertRaises(PhoneTaken):
            AdminRepository(db).create("asha", "h", "s", 310000, profile=dict(PHONE, email="a@example.com"))
        self.assertTrue(db.rolled_back)

    def test_an_admin_update_that_loses_the_race_becomes_phone_taken(self):
        db = FakeDb({"UPDATE tblAdmins": PHONE_INDEX_ERROR.replace("tblPlayers", "tblAdmins")})
        with self.assertRaises(PhoneTaken):
            AdminRepository(db).update(7, dict(PHONE))
        self.assertTrue(db.rolled_back)

    def test_a_new_admin_id_comes_from_the_insert_and_claims_the_phone(self):
        db = FakeDb()
        repository = AdminRepository(db)
        repository.get_by_id = lambda admin_id: admin_id
        self.assertEqual(repository.create("asha", "h", "s", 310000, profile=dict(PHONE, email="a@example.com")), 7)
        self.assertIn("INSERT INTO tblContactPhones (PhoneE164, OwnerKind, OwnerId) VALUES (%s, %s, %s)", db.statements)
        self.assertTrue(db.committed)

    def test_another_unique_key_is_not_reported_as_a_phone_conflict(self):
        db = FakeDb({"INSERT INTO tblAdmins": USERNAME_ERROR})
        with self.assertRaises(Exception) as ctx:
            AdminRepository(db).create("asha", "h", "s", 310000, profile={"email": "a@example.com"})
        self.assertNotIsInstance(ctx.exception, PhoneTaken)
        self.assertFalse(db.rolled_back)


class PlayerDeleteTests(unittest.TestCase):
    def test_deleting_a_player_releases_their_phone_claim(self):
        db = FakeDb()
        PlayerRepository(db).delete(7)
        self.assertTrue(any(s.startswith("DELETE FROM tblContactPhones WHERE OwnerKind = %s AND OwnerId = %s")
                            for s in db.statements))
        self.assertTrue(db.committed)


class OtpRaceTests(unittest.TestCase):
    def _service_with_code(self, code="123456"):
        store = MemoryPhoneOtpStore()
        store.issue("admin", 1, "+919876543210", _hash(code, "salt"), "salt", _utcnow() + timedelta(minutes=10))
        return PhoneOtpService(store=store), store

    def test_a_code_that_a_parallel_request_already_used_is_refused(self):
        service, store = self._service_with_code()
        stale = store.latest("admin", 1, "+919876543210")
        fresh = store.latest
        calls = iter([stale, stale])
        store.latest = lambda *args: next(calls, None) or fresh(*args)
        service.verify("admin", 1, "+919876543210", "123456")
        with self.assertRaises(HTTPException) as ctx:
            service.verify("admin", 1, "+919876543210", "123456")
        self.assertEqual(ctx.exception.detail, "That code was already used. Request a new one.")

    def test_parallel_wrong_codes_cannot_push_attempts_past_the_limit(self):
        service, store = self._service_with_code()
        stale = store.latest("admin", 1, "+919876543210")
        fresh = store.latest
        store.latest = lambda *args: stale
        for _ in range(8):
            with self.assertRaises(HTTPException):
                service.verify("admin", 1, "+919876543210", "000000")
        self.assertEqual(store.rows[0]["attempts"], 5)
        calls = iter([stale])
        store.latest = lambda *args: next(calls, None) or fresh(*args)
        with self.assertRaises(HTTPException) as ctx:
            service.verify("admin", 1, "+919876543210", "123456")
        self.assertEqual(ctx.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
