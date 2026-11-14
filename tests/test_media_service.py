import io
import unittest

from tests import test_auth_service as _env  # noqa: F401  (sets the environment before app imports)

from fastapi import HTTPException

from core.storage import StoredImage
from schemas.player_schema import PlayerCreateRequest, PlayerUpdateRequest
from schemas.team_schema import TeamCreateRequest, TeamUpdateRequest
from services.media_service import MAX_IMAGE_BYTES, MediaService, read_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


class FakeStorage:
    def __init__(self, fail_upload=False, fail_delete=False):
        self.objects = {}
        self.deleted = []
        self.fail_upload = fail_upload
        self.fail_delete = fail_delete
        self.counter = 0

    def upload(self, data, kind):
        if self.fail_upload:
            raise RuntimeError("storage down")
        self.counter += 1
        key = f"cpl/{kind}s/{self.counter}"
        self.objects[key] = data
        return StoredImage(url=f"https://img.test/{key}", key=key)

    def delete(self, key):
        if self.fail_delete:
            raise RuntimeError("storage down")
        self.deleted.append(key)
        self.objects.pop(key, None)

    def default_url(self, kind):
        return f"https://img.test/cpl/defaults/{kind}"


class FakePlayerRepository:
    def __init__(self, fail_save=False):
        self.rows = {1: {"url": None, "key": None}}
        self.fail_save = fail_save

    def get_avatar_key(self, player_id):
        row = self.rows.get(player_id)
        return (row is not None, row["key"] if row else None)

    def set_avatar(self, player_id, url, key):
        if self.fail_save:
            raise RuntimeError("database down")
        if player_id not in self.rows:
            return False
        self.rows[player_id] = {"url": url, "key": key}
        return True

    def get_by_id(self, player_id):
        return self.rows.get(player_id)


class FakeTeamRepository:
    def __init__(self):
        self.rows = {7: {"url": "https://legacy.test/logo.png", "key": None}}

    def get_logo_key(self, team_id):
        row = self.rows.get(team_id)
        return (row is not None, row["key"] if row else None)

    def set_logo(self, team_id, url, key):
        if team_id not in self.rows:
            return False
        self.rows[team_id] = {"url": url, "key": key}
        return True

    def get_by_id(self, team_id):
        return self.rows.get(team_id)


class ReadImageTests(unittest.TestCase):
    def test_accepts_jpeg_png_and_webp(self):
        for data in (JPEG, PNG, WEBP):
            self.assertEqual(read_image(io.BytesIO(data)), data)

    def test_rejects_non_images_even_with_an_image_name(self):
        for data in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"GIF89a" + b"\x00" * 20, b"%PDF-1.7"):
            with self.assertRaises(HTTPException) as raised:
                read_image(io.BytesIO(data))
            self.assertEqual(raised.exception.status_code, 400)

    def test_rejects_empty_upload(self):
        with self.assertRaises(HTTPException) as raised:
            read_image(io.BytesIO(b""))
        self.assertEqual(raised.exception.status_code, 400)

    def test_size_cap(self):
        self.assertEqual(len(read_image(io.BytesIO(PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG))))), MAX_IMAGE_BYTES)
        with self.assertRaises(HTTPException) as raised:
            read_image(io.BytesIO(PNG + b"\x00" * MAX_IMAGE_BYTES))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("5 MB", raised.exception.detail)


class PlayerAvatarTests(unittest.TestCase):
    def setUp(self):
        self.storage = FakeStorage()
        self.players = FakePlayerRepository()
        self.service = MediaService(self.storage, player_repository=self.players)

    def test_upload_stores_url_and_key(self):
        row = self.service.set_player_avatar(1, PNG)
        self.assertEqual(row, {"url": "https://img.test/cpl/players/1", "key": "cpl/players/1"})
        self.assertEqual(self.storage.deleted, [])

    def test_replace_uploads_first_then_deletes_the_old_image(self):
        self.service.set_player_avatar(1, PNG)
        row = self.service.set_player_avatar(1, JPEG)
        self.assertEqual(row["key"], "cpl/players/2")
        self.assertEqual(self.storage.deleted, ["cpl/players/1"])
        self.assertEqual(list(self.storage.objects), ["cpl/players/2"])

    def test_failed_save_deletes_the_new_upload_and_keeps_the_old(self):
        self.service.set_player_avatar(1, PNG)
        self.players.fail_save = True
        with self.assertRaises(RuntimeError):
            self.service.set_player_avatar(1, JPEG)
        self.assertEqual(self.players.rows[1]["key"], "cpl/players/1")
        self.assertEqual(list(self.storage.objects), ["cpl/players/1"])

    def test_storage_failure_is_a_bad_gateway_and_leaves_the_row(self):
        service = MediaService(FakeStorage(fail_upload=True), player_repository=self.players)
        with self.assertRaises(HTTPException) as raised:
            service.set_player_avatar(1, PNG)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIsNone(self.players.rows[1]["url"])

    def test_old_image_delete_failure_does_not_fail_the_replace(self):
        self.service.set_player_avatar(1, PNG)
        self.storage.fail_delete = True
        row = self.service.set_player_avatar(1, JPEG)
        self.assertEqual(row["key"], "cpl/players/2")

    def test_unknown_player_is_not_found_and_uploads_nothing(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.set_player_avatar(99, PNG)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.storage.objects, {})

    def test_remove_clears_the_row_and_deletes_the_image(self):
        self.service.set_player_avatar(1, PNG)
        row = self.service.remove_player_avatar(1)
        self.assertEqual(row, {"url": None, "key": None})
        self.assertEqual(self.storage.deleted, ["cpl/players/1"])

    def test_upload_without_storage_is_unavailable(self):
        service = MediaService(None, player_repository=self.players)
        with self.assertRaises(HTTPException) as raised:
            service.set_player_avatar(1, PNG)
        self.assertEqual(raised.exception.status_code, 503)


class TeamLogoTests(unittest.TestCase):
    def test_remove_legacy_url_needs_no_storage(self):
        teams = FakeTeamRepository()
        row = MediaService(None, team_repository=teams).remove_team_logo(7)
        self.assertEqual(row, {"url": None, "key": None})

    def test_upload_replaces_legacy_url(self):
        teams = FakeTeamRepository()
        storage = FakeStorage()
        row = MediaService(storage, team_repository=teams).set_team_logo(7, WEBP)
        self.assertEqual(row["key"], "cpl/teams/1")
        self.assertEqual(storage.deleted, [])

    def test_unknown_team_is_not_found(self):
        with self.assertRaises(HTTPException) as raised:
            MediaService(FakeStorage(), team_repository=FakeTeamRepository()).remove_team_logo(8)
        self.assertEqual(raised.exception.status_code, 404)


class DefaultsTests(unittest.TestCase):
    def test_defaults_with_storage(self):
        defaults = MediaService(FakeStorage()).defaults()
        self.assertEqual(defaults.player, "https://img.test/cpl/defaults/player")
        self.assertEqual(defaults.team, "https://img.test/cpl/defaults/team")
        self.assertTrue(defaults.uploads_enabled)

    def test_defaults_without_storage(self):
        defaults = MediaService(None).defaults()
        self.assertIsNone(defaults.player)
        self.assertFalse(defaults.uploads_enabled)


class RequestSchemaTests(unittest.TestCase):
    def test_picture_urls_are_not_settable_through_create_or_update(self):
        for model, payload in (
            (PlayerCreateRequest, {"first_name": "A", "last_name": "B", "avatar_url": "https://x.test/a.png"}),
            (PlayerUpdateRequest, {"avatar_url": "https://x.test/a.png"}),
            (TeamCreateRequest, {"name": "T", "logo_url": "https://x.test/t.png"}),
            (TeamUpdateRequest, {"logo_url": "https://x.test/t.png"}),
        ):
            dumped = model(**payload).model_dump(exclude_unset=True)
            self.assertNotIn("avatar_url", dumped)
            self.assertNotIn("logo_url", dumped)


class CloudinaryUrlTests(unittest.TestCase):
    def test_bad_url_is_rejected(self):
        from core.storage import CloudinaryStorage

        with self.assertRaises(RuntimeError):
            CloudinaryStorage("https://key:secret@cloud", "cpl")

    def test_delivery_urls_use_auto_format_and_quality(self):
        from core.storage import CloudinaryStorage

        storage = CloudinaryStorage("cloudinary://123:abc@demo-cloud", "cpl")
        url = storage.default_url("team")
        self.assertTrue(url.startswith("https://res.cloudinary.com/demo-cloud/image/upload/"))
        self.assertIn("f_auto", url)
        self.assertIn("q_auto", url)
        self.assertTrue(url.endswith("/cpl/defaults/team"))


if __name__ == "__main__":
    unittest.main()
