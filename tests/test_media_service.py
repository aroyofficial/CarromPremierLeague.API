import io
import unittest
from unittest.mock import patch

from tests import test_auth_service as _env  # noqa: F401  (sets the environment before app imports)

from fastapi import HTTPException

from core.storage import StoredImage
from schemas.player_schema import PlayerCreateRequest, PlayerUpdateRequest
from schemas.team_schema import TeamCreateRequest, TeamUpdateRequest
from services.media_service import (
    MAX_IMAGE_BYTES,
    MediaService,
    _GuardedRedirectHandler,
    fetch_public_image,
    normalize_picture_link,
    read_image,
    read_submitted_picture,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


class FakeStorage:
    def __init__(self, fail_upload=False, fail_delete=False):
        self.objects = {}
        self.owned = set()
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
        self.owned.add(key)
        return StoredImage(url=f"https://img.test/{key}", key=key)

    def owns(self, key):
        return key in self.owned

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

    def test_replace_does_not_delete_a_picture_that_is_not_cloudinary(self):
        self.players.rows[1] = {"url": "https://cdn.example/a.png", "key": "https://cdn.example/a.png"}
        row = self.service.set_player_avatar(1, PNG)
        self.assertEqual(self.storage.deleted, [])
        self.assertEqual(row["key"], "cpl/players/1")

    def test_remove_does_not_delete_a_picture_that_is_not_cloudinary(self):
        self.players.rows[1] = {"url": "https://cdn.example/a.png", "key": "https://cdn.example/a.png"}
        row = self.service.remove_player_avatar(1)
        self.assertEqual(row, {"url": None, "key": None})
        self.assertEqual(self.storage.deleted, [])

    def test_remove_owned_cloudinary_image_without_storage_is_unavailable(self):
        key = "cpl/players/" + ("ab" * 16)
        self.players.rows[1] = {"url": "https://res.cloudinary.com/demo/a.png", "key": key}
        service = MediaService(None, player_repository=self.players)
        with self.assertRaises(HTTPException) as raised:
            service.remove_player_avatar(1)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.players.rows[1]["key"], key)

    def test_link_is_stored_without_uploading_and_the_old_cloudinary_image_is_deleted(self):
        self.service.set_player_avatar(1, PNG)
        row = self.service.set_player_avatar_link(1, "https://cdn.example/a.png#cplcrop=0.1,0.2,0.5,0.4")
        self.assertEqual(row["url"], "https://cdn.example/a.png#cplcrop=0.1000,0.2000,0.5000,0.4000")
        self.assertIsNone(row["key"])
        self.assertEqual(self.storage.deleted, ["cpl/players/1"])
        self.assertEqual(self.storage.counter, 1)

    def test_link_does_not_delete_a_picture_that_is_not_cloudinary(self):
        self.players.rows[1] = {"url": "https://old.example/a.png", "key": "https://old.example/a.png"}
        row = self.service.set_player_avatar_link(1, "https://cdn.example/b.png")
        self.assertEqual(row["url"], "https://cdn.example/b.png")
        self.assertEqual(self.storage.deleted, [])
        self.assertEqual(self.storage.counter, 0)

    def test_link_rejects_a_local_or_broken_frame_and_leaves_the_row(self):
        for url in ("http://127.0.0.1/a.png", "https://cdn.example/a.png#cplcrop=2,0,0.2,0.2", "javascript:alert(1)"):
            with self.assertRaises(HTTPException) as raised:
                self.service.set_player_avatar_link(1, url)
            self.assertEqual(raised.exception.status_code, 400, url)
        self.assertIsNone(self.players.rows[1]["url"])
        self.assertEqual(self.storage.counter, 0)

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

    def test_remove_public_link_needs_no_storage_and_deletes_nothing(self):
        teams = FakeTeamRepository()
        teams.rows[7] = {"url": "https://cdn.example/logo.png", "key": "https://cdn.example/logo.png"}
        row = MediaService(None, team_repository=teams).remove_team_logo(7)
        self.assertEqual(row, {"url": None, "key": None})

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

    def test_delete_skips_anything_this_app_did_not_store(self):
        from core.storage import CloudinaryStorage

        storage = CloudinaryStorage("cloudinary://123:abc@demo-cloud", "cpl")
        owned = "cpl/players/" + ("cd" * 16)
        self.assertTrue(storage.owns(owned))
        self.assertTrue(storage.owns("cpl/teams/" + ("cd" * 16)))
        self.assertFalse(storage.owns("https://cdn.example/a.png"))
        self.assertFalse(storage.owns("cpl/defaults/player"))
        self.assertFalse(storage.owns("other/players/" + ("cd" * 16)))
        self.assertFalse(storage.owns("cpl/players/not-a-cloudinary-id"))
        with patch("cloudinary.uploader.destroy") as destroy:
            storage.delete("https://cdn.example/a.png")
            storage.delete("cpl/defaults/team")
            storage.delete("cpl/players/1")
            destroy.assert_not_called()


class FakeBody(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, data=PNG, headers=None, error=None):
        self.data = data
        self.headers = {"Content-Type": "image/png", **(headers or {})}
        self.error = error
        self.opened = []

    def open(self, request, timeout=None):
        self.opened.append(getattr(request, "full_url", request))
        self.timeout = timeout
        if self.error:
            raise self.error
        return FakeBody(self.data, self.headers)


def _public_dns(_host):
    return ["93.184.216.34"]


class PublicLinkTests(unittest.TestCase):
    def test_downloads_a_public_image(self):
        opener = FakeOpener()
        self.assertEqual(
            fetch_public_image("https://cdn.example/a.png", opener=opener, resolve=_public_dns),
            PNG,
        )
        self.assertEqual(opener.opened, ["https://cdn.example/a.png"])
        self.assertEqual(opener.timeout, 20)

    def test_refuses_local_and_private_targets_without_connecting(self):
        opener = FakeOpener()
        for url in (
            "http://127.0.0.1/a.png",
            "http://localhost/a.png",
            "file:///tmp/a.png",
            "https://user:secret@cdn.example/a.png",
            "http://10.1.2.3/a.png",
            "http://169.254.169.254/latest/meta-data",
        ):
            with self.assertRaises(HTTPException) as raised:
                fetch_public_image(url, opener=opener, resolve=_public_dns)
            self.assertEqual(raised.exception.status_code, 400, url)
        self.assertEqual(opener.opened, [])

    def test_refuses_a_name_that_resolves_to_a_private_address(self):
        opener = FakeOpener()
        with self.assertRaises(HTTPException) as raised:
            fetch_public_image(
                "https://cdn.example/a.png",
                opener=opener,
                resolve=lambda _host: ["10.0.0.8"],
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(opener.opened, [])

    def test_refuses_html_an_oversize_image_and_a_non_image(self):
        cases = (
            (FakeOpener(data=b"<html></html>", headers={"Content-Type": "text/html"}), "not an image"),
            (FakeOpener(headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)}), "5 MB"),
            (FakeOpener(data=b"GIF89a" + b"\x00" * 20, headers={"Content-Type": "application/octet-stream"}), "JPEG, PNG or WebP"),
        )
        for opener, expected in cases:
            with self.assertRaises(HTTPException) as raised:
                fetch_public_image("https://cdn.example/a.png", opener=opener, resolve=_public_dns)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn(expected, raised.exception.detail)

    def test_redirect_to_a_private_host_is_refused(self):
        import urllib.request

        handler = _GuardedRedirectHandler(_public_dns)
        request = urllib.request.Request("https://cdn.example/a.png")
        with self.assertRaises(HTTPException):
            handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/secret")
        followed = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/b.png")
        self.assertEqual(followed.full_url, "https://cdn.example/b.png")


class PictureLinkTests(unittest.TestCase):
    def test_frame_is_normalized_and_private_links_are_refused(self):
        self.assertEqual(
            normalize_picture_link(" https://cdn.example/a.png#cplcrop=0,0,1,1 "),
            "https://cdn.example/a.png#cplcrop=0.0000,0.0000,1.0000,1.0000",
        )
        for url in ("http://10.0.0.5/a.png", "https://user:pw@cdn.example/a.png", "https://cdn.example/a.png#not-a-frame"):
            with self.assertRaises(HTTPException) as raised:
                normalize_picture_link(url)
            self.assertEqual(raised.exception.status_code, 400, url)


class SubmittedPictureTests(unittest.TestCase):
    def test_file_or_link_but_not_both_or_neither(self):
        upload = type("Upload", (), {"filename": "a.png", "file": io.BytesIO(PNG)})()
        self.assertEqual(read_submitted_picture(upload, None), PNG)
        seen = {}

        def fetch(url):
            seen["url"] = url
            return JPEG

        self.assertEqual(read_submitted_picture(None, "  https://cdn.example/a.jpg ", fetch=fetch), JPEG)
        self.assertEqual(seen["url"], "https://cdn.example/a.jpg")
        for file, url in ((upload, "https://cdn.example/a.png"), (None, "  "), (None, None)):
            with self.assertRaises(HTTPException) as raised:
                read_submitted_picture(file, url, fetch=fetch)
            self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
