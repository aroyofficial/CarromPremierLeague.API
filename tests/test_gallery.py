import io
import struct
import unittest

from tests import test_auth_service as fakes  # noqa: F401  (sets the environment before app imports)

from fastapi import HTTPException

from core.storage import StoredImage
from schemas.auth_schema import AdminResponse
from schemas.gallery_schema import GalleryBulkRequest, GalleryOrderRequest, GalleryUpdateRequest
from services.gallery_service import (
    GalleryService,
    gallery_link,
    image_pixel_size,
    media_kind,
    page_by_position,
)


def _admin(*codes):
    return AdminResponse(id=7, username="curator", is_active=True, permissions=list(codes))


def _png(width, height):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + header


class MemoryGallery:
    def __init__(self):
        self.items = {}
        self.links = {}
        self.next_id = 1
        self.seasons = {1: {"name": "Shown", "show": True}, 2: {"name": "Hidden", "show": False}, 3: {"name": "Also", "show": True}}

    def known_seasons(self, season_ids):
        return {season_id for season_id in season_ids if season_id in self.seasons}

    def season_is_public(self, season_id):
        return season_id in self.seasons

    def insert(self, row):
        item_id = self.next_id
        self.next_id += 1
        self.items[item_id] = {
            "Id": item_id,
            "SourceType": row["source_type"],
            "MediaType": row.get("media_type") or "image",
            "Url": row["url"],
            "StorageKey": row["storage_key"],
            "Width": row["width"],
            "Height": row["height"],
            "Caption": row["caption"],
            "AltText": row["alt_text"],
            "TakenOn": row["taken_on"],
            "IsPublished": 0,
            "void": False,
        }
        return item_id

    def replace_seasons(self, item_id, season_ids):
        for key in list(self.links):
            if key[0] == item_id and key[1] not in season_ids:
                del self.links[key]
        for season_id in season_ids:
            if (item_id, season_id) in self.links:
                continue
            existing = [link["sort"] for (linked, season), link in self.links.items() if season == season_id]
            self.links[(item_id, season_id)] = {"sort": (max(existing) + 1 if existing else 0), "cover": False}

    def add_seasons(self, item_ids, season_ids):
        added = 0
        for item_id in item_ids:
            if item_id not in self.items or self.items[item_id]["void"]:
                continue
            before = self.season_ids_for([item_id]).get(item_id, [])
            missing = [season_id for season_id in season_ids if season_id not in before]
            if not missing:
                continue
            self.replace_seasons(item_id, before + missing)
            added += 1
        return added

    def get(self, item_id):
        item = self.items.get(item_id)
        if item is None or item["void"]:
            return None
        return item

    def get_public(self, item_id):
        item = self.get(item_id)
        if item is None or not item["IsPublished"]:
            return None
        if not any(self.season_is_public(season_id) for season_id in self.season_ids_for([item_id]).get(item_id, [])):
            return None
        row = dict(item)
        row["SortOrder"] = 0
        row["IsCover"] = 0
        return row

    def update(self, item_id, fields, admin_id):
        item = self.get(item_id)
        item.update(fields)

    def set_published(self, item_ids, published, admin_id):
        updated = 0
        for item_id in item_ids:
            item = self.get(item_id)
            if item is None:
                continue
            item["IsPublished"] = 1 if published else 0
            updated += 1
        return updated

    def void(self, item_id, admin_id):
        item = self.get(item_id)
        if item is None:
            return None
        for key, link in self.links.items():
            if key[0] == item_id:
                link["cover"] = False
        item["void"] = True
        return item

    def void_many(self, item_ids, admin_id):
        return [row for item_id in item_ids if (row := self.void(item_id, admin_id)) is not None]

    def season_ids_for(self, item_ids, public_only=False):
        grouped = {}
        for item_id, season_id in self.links:
            if item_id not in item_ids:
                continue
            if public_only and not self.season_is_public(season_id):
                continue
            grouped.setdefault(item_id, []).append(season_id)
        return grouped

    def _rows(self, season_id, published_only):
        rows = []
        for (item_id, linked_season), link in self.links.items():
            if season_id is not None and linked_season != season_id:
                continue
            item = self.items[item_id]
            if item["void"]:
                continue
            if published_only and not item["IsPublished"]:
                continue
            row = dict(item)
            row["SortOrder"] = link["sort"]
            row["IsCover"] = link["cover"]
            rows.append(row)
        rows.sort(key=lambda row: (row["SortOrder"], row["Id"]))
        return rows

    def list_public(self, season_id, after_sort, after_id, limit):
        if season_id not in self.seasons:
            return []
        rows = self._rows(season_id, True)
        if after_sort is not None:
            rows = [
                row
                for row in rows
                if row["SortOrder"] > after_sort or (row["SortOrder"] == after_sort and row["Id"] > after_id)
            ]
        return rows[:limit]

    def list_admin(self, season_id, status, caption_like, after_sort, after_id, limit):
        rows = self._rows(season_id, False) if season_id else [
            dict(item, SortOrder=0, IsCover=0)
            for item in self.items.values()
            if not item["void"]
        ]
        if not season_id:
            rows.sort(key=lambda row: row["Id"], reverse=True)
        if status == "published":
            rows = [row for row in rows if row["IsPublished"]]
        elif status == "draft":
            rows = [row for row in rows if not row["IsPublished"]]
        if after_sort is not None and season_id:
            rows = [
                row
                for row in rows
                if row["SortOrder"] > after_sort or (row["SortOrder"] == after_sort and row["Id"] > after_id)
            ]
        if not season_id and after_id is not None:
            rows = [row for row in rows if row["Id"] < after_id]
        return rows[:limit]

    def public_seasons(self):
        result = []
        for season_id, season in self.seasons.items():
            rows = self._rows(season_id, True)
            if not rows:
                continue
            cover = next((row for row in rows if row["IsCover"]), rows[0] if rows else None)
            result.append(
                {
                    "Id": season_id,
                    "Name": season["name"],
                    "PhotoCount": len(rows),
                    "CoverId": None if cover is None else cover["Id"],
                    "CoverSource": None if cover is None else cover["SourceType"],
                    "CoverMedia": None if cover is None else cover.get("MediaType") or "image",
                    "CoverUrl": None if cover is None else cover["Url"],
                    "CoverKey": None if cover is None else cover["StorageKey"],
                    "CoverWidth": None if cover is None else cover["Width"],
                    "CoverHeight": None if cover is None else cover["Height"],
                    "CoverCaption": None if cover is None else cover["Caption"],
                    "CoverAlt": None if cover is None else cover["AltText"],
                    "CoverTakenOn": None if cover is None else cover["TakenOn"],
                    "CoverSort": 0 if cover is None else cover["SortOrder"],
                }
            )
        return result

    def item_ids_for_season(self, season_id):
        return [item_id for (item_id, linked) in self.links if linked == season_id and not self.items[item_id]["void"]]

    def set_order(self, season_id, item_ids):
        for index, item_id in enumerate(item_ids):
            self.links[(item_id, season_id)]["sort"] = index

    def set_cover(self, season_id, item_id):
        if (item_id, season_id) not in self.links:
            return False
        for key, link in self.links.items():
            if key[1] == season_id:
                link["cover"] = key[0] == item_id
        return True


class FakeStorage:
    def __init__(self):
        self.deleted = []
        self.fail_delete = False
        self.uploads = 0

    def owns(self, key):
        if not isinstance(key, str):
            return False
        tail = key.rsplit("/", 1)[-1]
        return len(tail) == 32 and (key.startswith("cpl/gallery/") or key.startswith("cpl/videos/"))

    def upload(self, data, kind):
        self.uploads += 1
        key = "cpl/gallery/" + f"{self.uploads:032x}"
        return StoredImage(url=f"https://img.test/{key}", key=key)

    def upload_video(self, data):
        self.uploads += 1
        key = "cpl/videos/" + f"{self.uploads:032x}"
        return StoredImage(url=f"https://img.test/{key}.mp4", key=key)

    def delete(self, key):
        if self.fail_delete:
            raise RuntimeError("storage down")
        self.deleted.append(key)

    def gallery_variants(self, key):
        return {"thumbnail_url": f"https://img.test/thumb/{key}", "placeholder_url": f"https://img.test/blur/{key}"}


class GalleryRuleTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryGallery()
        self.storage = FakeStorage()
        self.service = GalleryService(self.repo, self.storage)
        self.curator = _admin("gallery.create", "gallery.update", "gallery.publish", "gallery.delete")

    def _link(self, url="https://cdn.example/photo.jpg", seasons="1", **extra):
        return self.service.create(
            self.curator,
            file=None,
            url=url,
            season_ids=seasons,
            caption=extra.get("caption"),
            alt_text=extra.get("alt_text"),
            width=extra.get("width", 800),
            height=extra.get("height", 600),
            taken_on=None,
        )

    def test_link_checks_reject_local_and_credential_urls(self):
        with self.assertRaises(HTTPException):
            gallery_link("http://127.0.0.1/secret.png")
        with self.assertRaises(HTTPException):
            gallery_link("javascript:alert(1)")
        with self.assertRaises(HTTPException):
            gallery_link("https://user:pass@cdn.example/a.png")
        self.assertEqual(gallery_link("https://cdn.example/a.png"), "https://cdn.example/a.png")
        self.assertEqual(
            gallery_link("https://cdn.example/a.jpg#cplcrop=0.1,0.2,0.5,0.4"),
            "https://cdn.example/a.jpg#cplcrop=0.1000,0.2000,0.5000,0.4000",
        )

    def test_png_pixel_size_is_read_from_the_header(self):
        self.assertEqual(image_pixel_size(_png(640, 480)), (640, 480))

    def test_item_can_be_saved_with_no_season(self):
        photo = self._link(url="https://cdn.example/unassigned.jpg", seasons="")
        self.assertEqual(photo.season_ids, [])
        listed = [item.id for item in self.service.admin_page(None, "all", None, None, 24).items]
        self.assertIn(photo.id, listed)
        assigned = self._link()
        cleared = self.service.update(
            assigned.id,
            GalleryUpdateRequest(season_ids=[]),
            self.curator,
            {"season_ids"},
        )
        self.assertEqual(cleared.season_ids, [])

    def test_new_photo_is_a_draft_and_a_link_is_not_deleted(self):
        photo = self._link()
        self.assertFalse(photo.is_published)
        self.assertEqual(photo.thumbnail_url, photo.url)
        self.assertIsNone(photo.placeholder_url)
        self.assertEqual(self.storage.uploads, 0)
        self.service.delete(photo.id, self.curator)
        self.assertEqual(self.storage.deleted, [])
        self.assertEqual(self.service.public_page(1, None, 24).items, [])

    def test_hidden_season_with_published_media_stays_on_the_public_gallery(self):
        shown = self._link(seasons="1")
        hidden = self._link(url="https://cdn.example/hidden.jpg", seasons="2")
        draft = self._link(url="https://cdn.example/draft.jpg", seasons="3")
        self.service.update(shown.id, GalleryUpdateRequest(is_published=True), self.curator, {"is_published"})
        self.service.update(hidden.id, GalleryUpdateRequest(is_published=True), self.curator, {"is_published"})
        public = self.service.public_page(1, None, 24)
        self.assertEqual([item.id for item in public.items], [shown.id])
        self.assertEqual([item.id for item in self.service.public_page(2, None, 24).items], [hidden.id])
        self.assertEqual(self.service.public_photo(hidden.id).season_ids, [2])
        names = [season.name for season in self.service.public_seasons()]
        self.assertEqual(names, ["Shown", "Hidden"])
        self.assertNotIn(draft.id, [item.id for item in self.service.public_page(3, None, 24).items])
        with self.assertRaises(HTTPException) as raised:
            self.service.public_photo(draft.id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_publishing_requires_the_publish_permission(self):
        photo = self._link()
        editor = _admin("gallery.update")
        with self.assertRaises(HTTPException) as raised:
            self.service.update(photo.id, GalleryUpdateRequest(is_published=True), editor, {"is_published"})
        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(self.repo.get(photo.id)["IsPublished"])

    def test_pages_follow_sort_order_then_id_without_skipping(self):
        photos = [self._link(url=f"https://cdn.example/{index}.jpg") for index in range(5)]
        for photo in photos:
            self.service.update(photo.id, GalleryUpdateRequest(is_published=True), self.curator, {"is_published"})
        rows = self.repo._rows(1, True)
        first, cursor = page_by_position(rows, None, 2)
        second, cursor_2 = page_by_position(rows, cursor, 2)
        third, done = page_by_position(rows, cursor_2, 2)
        seen = [row["Id"] for row in first + second + third]
        self.assertEqual(seen, [row["Id"] for row in rows])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIsNone(done)
        page = self.service.public_page(1, None, 2)
        self.assertEqual([item.id for item in page.items], seen[:2])
        nxt = self.service.public_page(1, page.next_cursor, 2)
        self.assertEqual([item.id for item in nxt.items], seen[2:4])

    def test_owned_upload_delete_is_logged_when_storage_fails(self):
        key = "cpl/gallery/" + ("ab" * 16)
        item_id = self.repo.insert(
            {
                "source_type": "upload",
                "url": "https://img.test/full",
                "storage_key": key,
                "width": 10,
                "height": 10,
                "caption": None,
                "alt_text": None,
                "taken_on": None,
                "admin_id": 7,
            }
        )
        self.repo.replace_seasons(item_id, [1])
        photo = self.service._saved(item_id)
        self.assertEqual(photo.thumbnail_url, f"https://img.test/thumb/{key}")
        self.assertEqual(photo.placeholder_url, f"https://img.test/blur/{key}")
        self.storage.fail_delete = True
        self.service.delete(item_id, self.curator)
        self.assertIsNone(self.repo.get(item_id))
        self.assertEqual(self.storage.deleted, [])

    def test_bulk_publish_hide_and_order(self):
        first = self._link(url="https://cdn.example/a.jpg")
        second = self._link(url="https://cdn.example/b.jpg")
        result = self.service.bulk(GalleryBulkRequest(action="publish", ids=[first.id, second.id]), self.curator)
        self.assertEqual(result.updated, 2)
        self.service.order(GalleryOrderRequest(season_id=1, item_ids=[second.id, first.id]))
        ordered = [row["Id"] for row in self.repo._rows(1, True)]
        self.assertEqual(ordered, [second.id, first.id])
        hidden = self.service.bulk(GalleryBulkRequest(action="hide", ids=[second.id]), self.curator)
        self.assertEqual(hidden.updated, 1)
        self.assertEqual([item.id for item in self.service.public_page(1, None, 24).items], [first.id])
        with self.assertRaises(HTTPException):
            self.service.bulk(GalleryBulkRequest(action="publish", ids=list(range(1, 102))), self.curator)

    def test_youtube_and_file_links_are_videos_and_photos_stay_images(self):
        photo = self._link()
        self.assertEqual(photo.media_type, "image")
        self.assertEqual(media_kind("https://cdn.example/a.jpg"), "image")
        clip = self._link(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(clip.media_type, "video")
        self.assertEqual(clip.thumbnail_url, "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")
        direct = self._link(url="https://cdn.example/match.mp4", width=None, height=None)
        self.assertEqual(direct.media_type, "video")
        self.assertEqual((direct.width, direct.height), (1920, 1080))

    def test_mp4_upload_uses_video_storage(self):
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8

        class _Upload:
            filename = "clip.mp4"
            file = io.BytesIO(data)

        clip = self.service.create(
            self.curator,
            file=_Upload(),
            url=None,
            season_ids="1",
            caption=None,
            alt_text=None,
            width=1280,
            height=720,
            taken_on=None,
        )
        self.assertEqual(clip.media_type, "video")
        self.assertEqual((clip.width, clip.height), (1280, 720))
        self.assertTrue(clip.url.endswith(".mp4"))
        self.service.delete(clip.id, self.curator)
        self.assertTrue(self.storage.deleted[0].startswith("cpl/videos/"))
