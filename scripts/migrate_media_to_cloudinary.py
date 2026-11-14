"""Move every player picture and team logo into Cloudinary and upload the default pictures.

Each picture comes from the image the web app shows today: the bundled web file
(read from the web repo's git history, so it still works after the files are
deleted) or, failing that, the URL in the database. Rows that already have a
storage key are skipped, so the script can be re-run safely.

Usage (from the API repo root, with CPL_CLOUDINARY_URL set in .env):
    python scripts/migrate_media_to_cloudinary.py --dry-run
    python scripts/migrate_media_to_cloudinary.py
"""
import argparse
import io
import re
import struct
import subprocess
import sys
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from core.database import Database  # noqa: E402
from core.storage import CloudinaryStorage, default_key  # noqa: E402
from services.media_service import MAX_IMAGE_BYTES, image_format  # noqa: E402

# Last web commit that still bundles the player and team images.
DEFAULT_WEB_REF = "3cc8ac8"
DEFAULT_WEB_REPO = ROOT.parent / "CarromPremierLeague.Web"


def player_slug(first_name, last_name):
    return re.sub(r"\s+", "-", f"{first_name or ''}-{last_name or ''}".lower().strip())


def team_slug(name):
    return (name or "").lower().replace(" ", "_")


def git_file(repo: Path, ref: str, path: str):
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


def download(url: str):
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "cpl-media-migration"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except Exception as error:
        print(f"    download failed: {error}")
        return None
    return data if len(data) <= MAX_IMAGE_BYTES else None


def silhouette_png(size=512):
    """A plain head-and-shoulders silhouette, drawn without image libraries."""
    background = (31, 41, 64)
    figure = (148, 163, 184)
    cx, head_cy, head_r = size / 2, size * 0.38, size * 0.17
    body_cy, body_rx, body_ry = size * 0.95, size * 0.34, size * 0.30
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            in_head = (x - cx) ** 2 + (y - head_cy) ** 2 <= head_r ** 2
            in_body = ((x - cx) / body_rx) ** 2 + ((y - body_cy) / body_ry) ** 2 <= 1
            rows.extend(figure if in_head or in_body else background)

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def pick_source(local_bytes, db_url):
    if local_bytes and image_format(local_bytes):
        return local_bytes, "web file"
    data = download(db_url)
    if data and image_format(data):
        return data, "database URL"
    return None, None


def upload_default(storage, kind, data, dry_run):
    import cloudinary.uploader

    public_id = default_key(storage.folder, kind)
    print(f"default {kind}: {len(data)} bytes -> {public_id}")
    if dry_run:
        return
    cloudinary.uploader.upload(
        io.BytesIO(data),
        resource_type="image",
        public_id=public_id,
        overwrite=True,
        invalidate=True,
        transformation=[{"width": 512, "height": 512, "crop": "limit"}],
    )


def migrate(table, rows, kind, local_for, save, storage, dry_run):
    moved = cleared = failed = 0
    for row in rows:
        label = row["Label"]
        if row["StorageKey"]:
            print(f"  {label}: already in storage, skipped")
            continue
        data, source = pick_source(local_for(row), row["Url"])
        if not data:
            if row["Url"] and not row["Url"].lower().startswith(("http://", "https://")):
                print(f"  {label}: bundled path {row['Url']} has no file, clearing")
                if not dry_run:
                    save(row["Id"], None, None)
                cleared += 1
            elif row["Url"]:
                print(f"  {label}: could not fetch {row['Url'][:60]}..., left as is")
                failed += 1
            else:
                print(f"  {label}: no picture, default will show")
            continue
        print(f"  {label}: uploading {len(data)} bytes from the {source}")
        if not dry_run:
            stored = storage.upload(data, kind)
            save(row["Id"], stored.url, stored.key)
        moved += 1
    print(f"{table}: {moved} uploaded, {cleared} cleared, {failed} could not be fetched")
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without uploading or writing")
    parser.add_argument("--web-repo", type=Path, default=DEFAULT_WEB_REPO, help="Path to the web repo")
    parser.add_argument("--web-ref", default=DEFAULT_WEB_REF, help="Web commit that still has the bundled images")
    args = parser.parse_args()

    if not settings.CLOUDINARY_URL:
        sys.exit("Set CPL_CLOUDINARY_URL in .env first.")
    storage = CloudinaryStorage(settings.CLOUDINARY_URL, settings.MEDIA_FOLDER)
    print(f"Cloudinary folder: {storage.folder}{'  (dry run)' if args.dry_run else ''}")

    if not git_file(args.web_repo, args.web_ref, "public/assets/images/teams/tbd.png"):
        sys.exit(f"Cannot read bundled images from {args.web_repo} at {args.web_ref}.")

    def local(path):
        return git_file(args.web_repo, args.web_ref, path)

    upload_default(storage, "player", silhouette_png(), args.dry_run)
    upload_default(storage, "team", local("public/assets/images/teams/tbd.png"), args.dry_run)

    Database.initialize()
    connection = Database.get_connection()
    try:
        from repositories.player_repository import PlayerRepository
        from repositories.team_repository import TeamRepository

        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT Id, Name AS Label, LogoUrl AS Url, LogoStorageKey AS StorageKey "
            "FROM tblTeams WHERE Void = 0 ORDER BY Id"
        )
        teams = cursor.fetchall()
        cursor.execute(
            "SELECT Id, FirstName, LastName, FirstName + ' ' + LastName AS Label, "
            "AvatarUrl AS Url, AvatarStorageKey AS StorageKey "
            "FROM tblPlayers WHERE Void = 0 ORDER BY Id"
        )
        players = cursor.fetchall()

        team_repository = TeamRepository(connection)
        player_repository = PlayerRepository(connection)
        failures = migrate(
            "Teams", teams, "team",
            lambda row: local(f"public/assets/images/teams/{team_slug(row['Label'])}.png"),
            team_repository.set_logo, storage, args.dry_run,
        )
        failures += migrate(
            "Players", players, "player",
            lambda row: local(f"public/assets/images/players/{player_slug(row['FirstName'], row['LastName'])}.png"),
            player_repository.set_avatar, storage, args.dry_run,
        )
    finally:
        connection.close()

    if failures:
        print("Some pictures could not be fetched; upload them from the admin pages.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
