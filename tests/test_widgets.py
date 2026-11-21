from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from tests import test_auth_service as fakes  # noqa: F401  (sets environment before app imports)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from core.permissions import APP_FORBIDDEN_SCOPES, MANAGE_WIDGETS
from core.security import create_access_token
from routes import auth_routes, widget_routes
from routes.auth_routes import _api_key_allowed_path
from schemas.auth_schema import AdminCreateRequest
from services.auth_service import AuthService
from services.widget_service import TOKEN_PREFIX, WidgetService, hash_token


class FakeWidgetRepository:
    def __init__(self):
        self.seq = 0
        self.rows: dict[int, dict] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def list_all(self) -> list[dict]:
        return [dict(row) for row in sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)]

    def get_by_id(self, widget_id: int) -> dict | None:
        row = self.rows.get(int(widget_id))
        return dict(row) if row else None

    def get_by_token_hash(self, token_hash: str) -> dict | None:
        for row in self.rows.values():
            if row["token_hash"] == token_hash:
                return dict(row)
        return None

    def create(self, *, name, widget_type, match_id, season_id, token_prefix, token_hash, expires_at, created_by) -> dict:
        self.seq += 1
        now = self._now()
        self.rows[self.seq] = {
            "id": self.seq,
            "name": name,
            "widget_type": widget_type,
            "match_id": match_id,
            "season_id": season_id,
            "token_prefix": token_prefix,
            "token_hash": token_hash,
            "is_active": True,
            "expires_at": expires_at,
            "last_used_at": None,
            "revoked_at": None,
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
        }
        return dict(self.rows[self.seq])

    def update(self, widget_id: int, changes: dict) -> dict | None:
        row = self.rows.get(int(widget_id))
        if row is None:
            return None
        if "name" in changes:
            row["name"] = changes["name"]
        if "expires_at" in changes:
            row["expires_at"] = changes["expires_at"]
        if "is_active" in changes:
            row["is_active"] = bool(changes["is_active"])
            row["revoked_at"] = None if row["is_active"] else (row["revoked_at"] or self._now())
        return dict(row)

    def rotate(self, widget_id: int, token_prefix: str, token_hash: str) -> dict | None:
        row = self.rows.get(int(widget_id))
        if row is None:
            return None
        row.update({"token_prefix": token_prefix, "token_hash": token_hash, "is_active": True, "revoked_at": None})
        return dict(row)

    def touch_last_used(self, widget_id: int) -> None:
        row = self.rows.get(int(widget_id))
        if row:
            row["last_used_at"] = self._now()


class FakeWidgetSource:
    def __init__(self):
        self.matches = {
            1: {
                "id": 1,
                "season_id": 5,
                "order": 1,
                "scheduled_date": "2026-11-21",
                "category": 1,
                "status": 2,
                "outcome": 1,
                "net_points": 3,
                "duration": 1200,
                "extra": 0,
                "golden_strike": False,
                "toss_outcome": 1,
                "started_at": "2026-11-21T12:00:00Z",
                "extra_started_at": None,
                "team1": 10,
                "team2": 20,
            }
        }
        self._stats = {
            1: [
                {"match_id": 1, "player_id": 100, "coins_pocketed": 5, "opponent_coins_pocketed": 0, "single_fines": 1,
                 "double_fines": 0, "strikers_pocketed": 0, "coins_fined": 1, "shots_taken": 9},
                {"match_id": 1, "player_id": 200, "coins_pocketed": 3, "opponent_coins_pocketed": 1, "single_fines": 0,
                 "double_fines": 0, "strikers_pocketed": 1, "coins_fined": 0, "shots_taken": 7},
            ]
        }
        self._teams = {10: {"name": "Dum Dum Diamonds", "logo_url": None}, 20: {"name": "Kolkata Kings", "logo_url": None}}
        self._rosters = {
            (5, 10): [{"player_id": 100, "first_name": "Ana", "last_name": "Lee"}],
            (5, 20): [{"player_id": 200, "first_name": "Ben", "last_name": "Cole"}],
        }
        self._seasons = {5: {"id": 5, "name": "Season 5"}}

    def match_exists(self, match_id: int) -> bool:
        return match_id in self.matches

    def season_exists(self, season_id: int) -> bool:
        return season_id in self._seasons

    def match(self, match_id: int) -> dict | None:
        found = self.matches.get(match_id)
        return dict(found) if found else None

    def stats(self, match_id: int) -> list[dict]:
        return [dict(row) for row in self._stats.get(match_id, [])]

    def season(self, season_id: int) -> dict | None:
        found = self._seasons.get(season_id)
        return dict(found) if found else None

    def season_matches(self, season_id: int) -> list[dict]:
        return [dict(m) for m in self.matches.values() if m["season_id"] == season_id]

    def teams(self) -> dict[int, dict]:
        return {key: dict(value) for key, value in self._teams.items()}

    def team_roster(self, season_id: int, team_id: int) -> list[dict]:
        return [dict(p) for p in self._rosters.get((season_id, team_id), [])]


def _build_harness():
    admins = fakes.FakeAdminRepository()
    perms = fakes.FakePermissionRepository(admins)
    auth_service = AuthService(admins, perms)
    owner = auth_service.bootstrap_admin(AdminCreateRequest(username="owner", password="correct-horse-battery"))

    repo = FakeWidgetRepository()
    source = FakeWidgetSource()

    app = FastAPI()
    app.state.widget_service = WidgetService(repo, source)
    app.dependency_overrides[auth_routes.get_service] = lambda: auth_service
    app.dependency_overrides[get_db] = lambda: iter([None])
    app.include_router(auth_routes.router)
    app.include_router(widget_routes.router)
    return TestClient(app), auth_service, repo, source, owner


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.client, self.auth_service, self.repo, self.source, self.owner = _build_harness()
        self._phones = 9400000300

    def _bearer(self, admin) -> dict[str, str]:
        record = self.auth_service.repository.records[admin.id]
        token = create_access_token(admin.id, admin.username, record.get("TokenVersion", 0))
        return {"Authorization": f"Bearer {token}"}

    def _make_admin(self, username: str, permissions: list[str]):
        self._phones += 1
        created = self.auth_service.create_admin(
            AdminCreateRequest(
                username=username,
                password="correct-horse-battery",
                permissions=permissions,
                email=f"{username}@example.com",
                phone_code="+91",
                phone_number=str(self._phones),
            ),
            self.owner,
        )
        self.auth_service.repository.records[created.id]["IsActive"] = True
        self.auth_service.repository.records[created.id]["MustChangePassword"] = False
        return created

    def _create(self, **body) -> dict:
        payload = {"name": "Main stream", "widget_type": "scorecard", "match_id": 1}
        payload.update(body)
        response = self.client.post("/widgets", json=payload, headers=self._bearer(self.owner))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]

    def _snapshot(self, token: str):
        return self.client.get(f"/widgets/t/{token}")

    def test_each_widget_type_returns_public_data_and_no_secrets(self):
        scorecard = self._create(name="Scorecard", widget_type="scorecard", match_id=1)
        toss = self._create(name="Toss", widget_type="toss", match_id=1)
        fixtures = self._create(name="Fixtures", widget_type="fixtures", match_id=None, season_id=5)

        card = self._snapshot(scorecard["token"]).json()["data"]
        self.assertEqual(card["type"], "scorecard")
        self.assertEqual(card["match"]["team1"]["name"], "Dum Dum Diamonds")
        self.assertEqual(card["players"]["team1"][0]["name"], "Ana Lee")
        self.assertEqual(card["players"]["team1"][0]["coins_pocketed"], 5)
        self.assertEqual(card["players"]["team2"][0]["name"], "Ben Cole")
        self.assertEqual(card["players"]["team2"][0]["opponent_coins_pocketed"], 1)

        tossed = self._snapshot(toss["token"]).json()["data"]
        self.assertEqual(tossed["type"], "toss")
        self.assertEqual(tossed["match"]["toss_outcome"], 1)
        self.assertNotIn("players", tossed)

        listed = self._snapshot(fixtures["token"]).json()["data"]
        self.assertEqual(listed["type"], "fixtures")
        self.assertEqual(listed["season"], {"id": 5, "name": "Season 5"})
        self.assertEqual(listed["matches"][0]["team2"]["name"], "Kolkata Kings")

        for body in (card, tossed, listed):
            text = json.dumps(body)
            for secret in ("token", "token_hash", "created_by", "cpl_widget_"):
                self.assertNotIn(secret, text)

    def test_snapshot_match_fields_match_the_public_match_data(self):
        created = self._create()
        card = self._snapshot(created["token"]).json()["data"]
        public = self.source.match(1)
        for field in ("status", "outcome", "net_points", "duration", "extra", "golden_strike", "toss_outcome", "order"):
            self.assertEqual(card["match"][field], public[field], field)

    def test_token_is_shown_once_and_stored_only_as_a_hash(self):
        created = self._create()
        token = created["token"]
        self.assertTrue(token.startswith(TOKEN_PREFIX))
        stored = self.repo.rows[created["id"]]
        self.assertEqual(stored["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(stored, default=str))

        listed = self.client.get("/widgets", headers=self._bearer(self.owner)).json()["data"]["widgets"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertEqual(listed[0]["token_prefix"], token[:18])

    def test_turning_a_link_off_stops_it_and_on_restores_it(self):
        created = self._create()
        token = created["token"]
        self.assertEqual(self._snapshot(token).status_code, 200)

        off = self.client.patch(f"/widgets/{created['id']}", json={"is_active": False}, headers=self._bearer(self.owner))
        self.assertEqual(off.json()["data"]["status"], "revoked")
        self.assertEqual(self._snapshot(token).status_code, 401)

        on = self.client.patch(f"/widgets/{created['id']}", json={"is_active": True}, headers=self._bearer(self.owner))
        self.assertEqual(on.json()["data"]["status"], "active")
        self.assertEqual(self._snapshot(token).status_code, 200)

    def test_expired_link_returns_401(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        created = self._create(expires_at=future.isoformat())
        self.assertEqual(self._snapshot(created["token"]).status_code, 200)

        self.repo.rows[created["id"]]["expires_at"] = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        self.assertEqual(self._snapshot(created["token"]).status_code, 401)
        listed = self.client.get("/widgets", headers=self._bearer(self.owner)).json()["data"]["widgets"]
        self.assertEqual(listed[0]["status"], "expired")

    def test_expiry_past_is_rejected_on_create(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        response = self.client.post(
            "/widgets",
            json={"name": "Old", "widget_type": "toss", "match_id": 1, "expires_at": past.isoformat()},
            headers=self._bearer(self.owner),
        )
        self.assertEqual(response.status_code, 400)

    def test_rotate_invalidates_the_old_token(self):
        created = self._create()
        old = created["token"]
        rotated = self.client.post(f"/widgets/{created['id']}/rotate", headers=self._bearer(self.owner)).json()["data"]
        self.assertNotEqual(rotated["token"], old)
        self.assertEqual(self._snapshot(old).status_code, 401)
        self.assertEqual(self._snapshot(rotated["token"]).status_code, 200)

    def test_unknown_or_malformed_tokens_get_401_without_detail(self):
        for token in ("nope", TOKEN_PREFIX + "x" * 20, TOKEN_PREFIX + "y" * 40):
            response = self._snapshot(token)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("cpl_widget_", response.text)

    def test_snapshot_is_not_cached(self):
        created = self._create()
        response = self._snapshot(created["token"])
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_widget_token_cannot_call_admin_routes(self):
        created = self._create()
        response = self.client.get("/widgets", headers={"X-API-Key": created["token"]})
        self.assertIn(response.status_code, (401, 403))
        patch = self.client.patch(f"/widgets/{created['id']}", json={"name": "x"}, headers={"Authorization": f"Bearer {created['token']}"})
        self.assertIn(patch.status_code, (401, 403))

    def test_api_keys_cannot_reach_widget_management(self):
        self.assertFalse(_api_key_allowed_path("/widgets"))
        self.assertFalse(_api_key_allowed_path("/widgets/1/rotate"))
        self.assertIn(MANAGE_WIDGETS, APP_FORBIDDEN_SCOPES)

    def test_admin_without_widgets_manage_is_refused(self):
        viewer = self._make_admin("viewer", ["settings.update"])
        response = self.client.get("/widgets", headers=self._bearer(viewer))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/widgets",
            json={"name": "Nope", "widget_type": "toss", "match_id": 1},
            headers=self._bearer(viewer),
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_with_widgets_manage_can_manage(self):
        manager = self._make_admin("streamer", [MANAGE_WIDGETS])
        response = self.client.post(
            "/widgets",
            json={"name": "Stream", "widget_type": "toss", "match_id": 1},
            headers=self._bearer(manager),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["created_by"], manager.id)

    def test_create_rejects_mismatched_targets_and_unknown_matches(self):
        headers = self._bearer(self.owner)
        cases = [
            {"name": "A", "widget_type": "fixtures", "match_id": 1},
            {"name": "B", "widget_type": "fixtures", "season_id": 5, "match_id": 1},
            {"name": "C", "widget_type": "scorecard", "season_id": 5},
            {"name": "D", "widget_type": "scorecard", "match_id": 999},
            {"name": "E", "widget_type": "fixtures", "season_id": 999},
            {"name": "   ", "widget_type": "toss", "match_id": 1},
        ]
        for body in cases:
            response = self.client.post("/widgets", json=body, headers=headers)
            self.assertIn(response.status_code, (400, 422), body)

    def test_patch_without_expiry_keeps_it_and_null_clears_it(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        created = self._create(expires_at=future.isoformat())
        headers = self._bearer(self.owner)

        renamed = self.client.patch(f"/widgets/{created['id']}", json={"name": "Renamed"}, headers=headers).json()["data"]
        self.assertEqual(renamed["name"], "Renamed")
        self.assertIsNotNone(renamed["expires_at"])

        cleared = self.client.patch(f"/widgets/{created['id']}", json={"expires_at": None}, headers=headers).json()["data"]
        self.assertIsNone(cleared["expires_at"])

    def test_missing_widget_returns_404(self):
        response = self.client.patch("/widgets/999", json={"name": "x"}, headers=self._bearer(self.owner))
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/widgets/999/rotate", headers=self._bearer(self.owner))
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
