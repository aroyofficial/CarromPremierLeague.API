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

from fastapi import HTTPException

from core.phone_numbers import PhoneTaken
from schemas.player_schema import PlayerCreateRequest, PlayerResponse, PlayerUpdateRequest
from services.player_service import PlayerService


class FakeCountries:
    def validate_country_exists(self, country_id):
        return None


class FakePlayerRepository:
    def __init__(self):
        self.players = {}
        self.claims = {}  # e164 -> (kind, owner_id)
        self.next_id = 1
        self.last_phone = "unset"

    def get_by_id(self, player_id):
        row = self.players.get(player_id)
        if not row:
            return None
        return PlayerResponse(id=player_id, first_name=row["first"], last_name=row["last"],
                              date_of_birth=None, avatar_url=None, nationality_id=None)

    def get_phone(self, player_id):
        row = self.players.get(player_id, {})
        return row.get("phone_code"), row.get("phone_number")

    def find_phone(self, e164):
        return self.claims.get(e164)

    def create(self, request: PlayerCreateRequest, phone=None):
        self.last_phone = phone
        player_id = self.next_id
        self.next_id += 1
        self.players[player_id] = {"first": request.first_name, "last": request.last_name}
        if phone and phone["phone_e164"]:
            if self.claims.get(phone["phone_e164"]) not in (None, ("player", player_id)):
                raise PhoneTaken()
            self.claims[phone["phone_e164"]] = ("player", player_id)
        self.players[player_id].update(phone or {})
        return self.get_by_id(player_id)

    def update(self, player_id, request: PlayerUpdateRequest, phone=None):
        self.last_phone = phone
        if phone is not None:
            self.players[player_id].update(phone)
            self.claims = {k: v for k, v in self.claims.items() if v != ("player", player_id)}
            if phone["phone_e164"]:
                self.claims[phone["phone_e164"]] = ("player", player_id)
        return self.get_by_id(player_id)


class PlayerPhoneTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakePlayerRepository()
        self.service = PlayerService(self.repository, FakeCountries())

    def _create(self, code="+91", number=None):
        return self.service.create(PlayerCreateRequest(first_name="Asha", last_name="Rao", phone_code=code, phone_number=number))

    def test_a_player_without_a_phone_is_stored_with_no_phone(self):
        self.service.create(PlayerCreateRequest(first_name="Asha", last_name="Rao"))
        self.assertEqual(self.repository.last_phone, {"phone_code": None, "phone_number": None, "phone_e164": None})

    def test_a_messy_number_is_stored_in_canonical_form(self):
        self._create("+91", "09876 543210")
        self.assertEqual(self.repository.last_phone, {"phone_code": "+91", "phone_number": "9876543210", "phone_e164": "+919876543210"})

    def test_a_duplicate_number_is_rejected_before_saving(self):
        self._create("+91", "9876543210")
        with self.assertRaises(HTTPException) as ctx:
            self._create("0091", "9876543210")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "That phone number is already used.")

    def test_a_number_owned_by_an_admin_is_rejected(self):
        self.repository.claims["+919876543210"] = ("admin", 7)
        with self.assertRaises(HTTPException) as ctx:
            self._create("+91", "9876543210")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_race_that_slips_past_the_check_still_returns_409(self):
        self.repository.claims["+919876543210"] = ("admin", 7)
        original = self.repository.find_phone
        self.repository.find_phone = lambda e164: None  # the pre-check misses it
        with self.assertRaises(HTTPException) as ctx:
            self._create("+91", "9876543210")
        self.assertEqual(ctx.exception.status_code, 409)
        self.repository.find_phone = original

    def test_one_sided_or_invalid_numbers_are_rejected(self):
        for code, number in (("+91", None), (None, "9876543210"), ("+91", "12345"), ("+91", "abcdefghij")):
            with self.subTest(code=code, number=number):
                with self.assertRaises(HTTPException) as ctx:
                    self._create(code, number)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_a_player_can_keep_their_own_number_on_update(self):
        self._create("+91", "9876543210")
        self.service.update(1, PlayerUpdateRequest(first_name="Asha", phone_code="+91", phone_number="9876543210"))
        self.assertEqual(self.repository.last_phone["phone_e164"], "+919876543210")

    def test_clearing_the_phone_releases_it(self):
        self._create("+91", "9876543210")
        self.service.update(1, PlayerUpdateRequest(phone_code=None, phone_number=None))
        self.assertEqual(self.repository.last_phone, {"phone_code": None, "phone_number": None, "phone_e164": None})
        self.assertNotIn("+919876543210", self.repository.claims)

    def test_updating_a_name_leaves_the_stored_phone_alone(self):
        self._create("+91", "9876543210")
        self.service.update(1, PlayerUpdateRequest(first_name="Asha"))
        self.assertIsNone(self.repository.last_phone)

    def test_updating_a_player_to_another_players_number_is_rejected(self):
        self._create("+91", "9876543210")
        self.service.create(PlayerCreateRequest(first_name="Ravi", last_name="Kumar"))
        with self.assertRaises(HTTPException) as ctx:
            self.service.update(2, PlayerUpdateRequest(phone_code="+91", phone_number="9876543210"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_public_player_responses_do_not_carry_the_phone(self):
        self.assertNotIn("phone", " ".join(PlayerResponse.model_fields))


if __name__ == "__main__":
    unittest.main()
