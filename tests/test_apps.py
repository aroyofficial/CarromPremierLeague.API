from datetime import datetime, timedelta, timezone
import unittest

from tests import test_auth_service as fakes  # noqa: F401  (sets environment before app imports)

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from core.permissions import (
    APP_FORBIDDEN_SCOPES,
    MANAGE_APPS,
    MANAGE_WEBHOOKS,
    MANAGE_WIDGETS,
    ROLES,
)
from core.realtime import STAFF_CHANNEL, staff_notices
from core.security import create_access_token
from routes import app_routes, auth_routes
from routes.auth_routes import get_current_admin, require_permission
from schemas.auth_schema import AdminCreateRequest
from services.app_service import AppService, hash_api_key
from services.auth_service import AuthService


class FakeAppRepository:
    def __init__(self):
        self._seq = 0
        self.rows: dict[int, dict] = {}

    def _copy(self, row: dict) -> dict:
        copied = dict(row)
        copied["permissions"] = list(row["permissions"])
        return copied

    def list_all(self) -> list[dict]:
        return [
            self._copy(row)
            for row in sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)
            if not row["void"]
        ]

    def get_by_id(self, app_id: int) -> dict | None:
        row = self.rows.get(int(app_id))
        if not row or row["void"]:
            return None
        return self._copy(row)

    def get_by_key_hash(self, key_hash: str) -> dict | None:
        for row in self.rows.values():
            if row["key_hash"] == key_hash:
                return self._copy(row)
        return None

    def create(
        self,
        *,
        name: str,
        description: str | None,
        key_prefix: str,
        key_hash: str,
        permissions: list[str],
        expires_at: datetime | None,
        created_by: int,
    ) -> dict:
        self._seq += 1
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        record = {
            "id": self._seq,
            "name": name,
            "description": description,
            "key_prefix": key_prefix,
            "key_hash": key_hash,
            "permissions": list(permissions),
            "is_active": True,
            "void": False,
            "expires_at": expires_at,
            "last_used_at": None,
            "revoked_at": None,
            "created_at": now,
            "updated_at": now,
            "created_by": int(created_by),
        }
        self.rows[self._seq] = record
        return self._copy(record)

    def update(self, app_id: int, changes: dict) -> dict | None:
        row = self.rows.get(int(app_id))
        if not row or row["void"]:
            return None
        for field in ("name", "description", "is_active", "expires_at"):
            if field in changes:
                row[field] = changes[field]
        if "permissions" in changes and changes["permissions"] is not None:
            row["permissions"] = list(changes["permissions"])
        row["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return self._copy(row)

    def rotate_key(self, app_id: int, key_prefix: str, key_hash: str) -> dict | None:
        row = self.rows.get(int(app_id))
        if not row or row["void"]:
            return None
        row["key_prefix"] = key_prefix
        row["key_hash"] = key_hash
        row["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return self._copy(row)

    def revoke(self, app_id: int) -> bool:
        row = self.rows.get(int(app_id))
        if not row or row["void"]:
            return False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row["void"] = True
        row["is_active"] = False
        row["revoked_at"] = now
        row["updated_at"] = now
        return True

    def touch_last_used(self, app_id: int) -> None:
        row = self.rows.get(int(app_id))
        if row:
            row["last_used_at"] = datetime.now(timezone.utc).replace(tzinfo=None)


def _build_test_harness():
    admins = fakes.FakeAdminRepository()
    perms = fakes.FakePermissionRepository(admins)
    auth_service = AuthService(admins, perms)
    owner = auth_service.bootstrap_admin(
        AdminCreateRequest(username="owner", password="correct-horse-battery")
    )

    app_repo = FakeAppRepository()
    app_service = AppService(app_repo)

    app = FastAPI()
    app.state.app_service = app_service
    app.dependency_overrides[auth_routes.get_service] = lambda: auth_service
    app.dependency_overrides[get_db] = lambda: iter([None])
    app.include_router(auth_routes.router)
    app.include_router(app_routes.router)

    @app.post("/teams")
    def create_team(actor=Depends(require_permission("teams.create"))):
        return {"ok": True, "actor": actor.username}

    @app.post("/players")
    def create_player(actor=Depends(require_permission("players.create"))):
        return {"ok": True, "actor": actor.username}

    @app.get("/webhooks")
    def list_webhooks(actor=Depends(require_permission(MANAGE_WEBHOOKS))):
        return {"ok": True, "actor": actor.username}

    @app.get("/any-signed-in")
    def signed_in(actor=Depends(get_current_admin)):
        return {"username": actor.username, "permissions": actor.permissions}

    return TestClient(app), auth_service, app_repo, owner


class AppPermissionsAndApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.client, self.auth_service, self.repo, self.owner = _build_test_harness()
        self._phones = 9400000100

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

    def test_integration_manager_role_grants_webhooks_apps_and_widgets_only(self):
        role = next(r for r in ROLES if r.code == "integration_manager")
        self.assertEqual(set(role.permissions), {MANAGE_WEBHOOKS, MANAGE_APPS, MANAGE_WIDGETS})

    def test_apps_endpoints_require_apps_manage_permission(self):
        settings_only = self._make_admin("settings.only", ["settings.update", MANAGE_WEBHOOKS])
        headers = self._bearer(settings_only)

        self.assertEqual(self.client.get("/apps", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.post("/apps", json={"name": "Bot", "permissions": []}, headers=headers).status_code,
            403,
        )
        self.assertEqual(self.client.patch("/apps/1", json={"name": "Renamed"}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/apps/1/rotate", headers=headers).status_code, 403)
        self.assertEqual(self.client.delete("/apps/1", headers=headers).status_code, 403)

    def test_scope_escalation_and_forbidden_scopes_are_rejected(self):
        integrator = self._make_admin("integrator", [MANAGE_APPS, "teams.create", "matches.update"])
        headers = self._bearer(integrator)

        # Cannot grant a permission the actor does not hold
        unheld = self.client.post(
            "/apps",
            json={"name": "Escalation", "permissions": ["teams.create", "seasons.delete"]},
            headers=headers,
        )
        self.assertEqual(unheld.status_code, 403)

        # Cannot grant account / privilege-escalation scopes even as owner
        owner_headers = self._bearer(self.owner)
        for forbidden in sorted(APP_FORBIDDEN_SCOPES):
            with self.subTest(forbidden=forbidden):
                res = self.client.post(
                    "/apps",
                    json={"name": "BadScope", "permissions": [forbidden]},
                    headers=owner_headers,
                )
                self.assertEqual(res.status_code, 400)

        # Child scope requires its parent scope
        missing_parent = self.client.post(
            "/apps",
            json={"name": "ChildOnly", "permissions": ["matches.scorecard"]},
            headers=owner_headers,
        )
        self.assertEqual(missing_parent.status_code, 400)

    def test_one_time_key_is_hashed_at_rest_and_never_returned_on_list(self):
        integrator = self._make_admin("integrator2", [MANAGE_APPS, "teams.create"])
        headers = self._bearer(integrator)

        created_res = self.client.post(
            "/apps",
            json={"name": "Scoreboard Sync", "description": "Reads & adds teams", "permissions": ["teams.create"]},
            headers=headers,
        )
        self.assertEqual(created_res.status_code, 201)
        payload = created_res.json()["data"]
        raw_key = payload["api_key"]
        self.assertTrue(raw_key.startswith("cpl_live_"))
        self.assertEqual(payload["key_prefix"], raw_key[:17])

        # Stored record contains SHA-256 digest, never raw key
        stored = self.repo.rows[payload["id"]]
        self.assertEqual(stored["key_hash"], hash_api_key(raw_key))
        self.assertNotEqual(stored["key_hash"], raw_key)

        # Listing apps never exposes api_key or key_hash
        listed = self.client.get("/apps", headers=headers).json()["data"]["apps"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("api_key", listed[0])
        self.assertNotIn("key_hash", listed[0])

    def test_api_key_authenticates_permitted_league_routes_and_blocks_missing_or_account_paths(self):
        headers = self._bearer(self.owner)
        created = self.client.post(
            "/apps",
            json={"name": "Team Writer", "permissions": ["teams.create"]},
            headers=headers,
        ).json()["data"]
        key_headers = {"X-API-Key": created["api_key"]}

        # Self-inspection works
        me = self.client.get("/apps/me", headers=key_headers)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["permissions"], ["teams.create"])

        # Allowed scope succeeds and updates last_used_at
        ok = self.client.post("/teams", headers=key_headers)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["actor"].startswith("app:cpl_live_"))
        self.assertIsNotNone(self.repo.rows[created["id"]]["last_used_at"])

        # Missing scope is rejected with 403
        missing = self.client.post("/players", headers=key_headers)
        self.assertEqual(missing.status_code, 403)

        # Account and integration management paths reject X-API-Key with 403
        for blocked_path in ("/auth/me", "/admins", "/apps", "/webhooks"):
            with self.subTest(path=blocked_path):
                res = self.client.get(blocked_path, headers=key_headers)
                self.assertEqual(res.status_code, 403)

    def test_read_only_app_key_can_verify_itself_via_apps_me(self):
        headers = self._bearer(self.owner)
        created = self.client.post(
            "/apps",
            json={"name": "Read Only Consumer", "permissions": []},
            headers=headers,
        ).json()["data"]
        me = self.client.get("/apps/me", headers={"X-API-Key": created["api_key"]})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["permissions"], [])

    def test_rotation_pause_revocation_and_expiry_invalidate_key(self):
        headers = self._bearer(self.owner)

        # Past expiry is rejected on creation
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        bad_exp = self.client.post(
            "/apps",
            json={"name": "Already Expired", "permissions": [], "expires_at": past},
            headers=headers,
        )
        self.assertEqual(bad_exp.status_code, 400)

        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        created = self.client.post(
            "/apps",
            json={"name": "Rotatable App", "permissions": ["teams.create"], "expires_at": future},
            headers=headers,
        ).json()["data"]
        app_id = created["id"]
        first_key = created["api_key"]

        # Rotate replaces key atomically
        rotated = self.client.post(f"/apps/{app_id}/rotate", headers=headers).json()["data"]
        second_key = rotated["api_key"]
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(self.client.post("/teams", headers={"X-API-Key": first_key}).status_code, 401)
        self.assertEqual(self.client.post("/teams", headers={"X-API-Key": second_key}).status_code, 200)

        # Pausing app rejects key until re-enabled
        self.client.patch(f"/apps/{app_id}", json={"is_active": False}, headers=headers)
        self.assertEqual(self.client.post("/teams", headers={"X-API-Key": second_key}).status_code, 401)
        self.client.patch(f"/apps/{app_id}", json={"is_active": True}, headers=headers)
        self.assertEqual(self.client.post("/teams", headers={"X-API-Key": second_key}).status_code, 200)

        # Expired timestamp rejects key
        self.repo.rows[app_id]["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
        expired_res = self.client.post("/teams", headers={"X-API-Key": second_key})
        self.assertEqual(expired_res.status_code, 401)
        self.assertIn("expired", expired_res.json()["detail"].lower())
        self.repo.rows[app_id]["expires_at"] = None

        # Revoking deletes from active list and rejects key
        revoked = self.client.delete(f"/apps/{app_id}", headers=headers)
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(self.client.post("/teams", headers={"X-API-Key": second_key}).status_code, 401)
        self.assertEqual(self.client.get("/apps", headers=headers).json()["data"]["apps"], [])

    def test_realtime_staff_notices_emit_integrations_area_for_apps_and_webhooks(self):
        self.assertEqual(staff_notices("POST", "/apps", actor_id=5), [(STAFF_CHANNEL, "integrations")])
        self.assertEqual(staff_notices("DELETE", "/apps/3", actor_id=5), [(STAFF_CHANNEL, "integrations")])
        self.assertEqual(staff_notices("POST", "/webhooks", actor_id=5), [(STAFF_CHANNEL, "integrations")])
        self.assertEqual(staff_notices("PATCH", "/webhooks/2", actor_id=5), [(STAFF_CHANNEL, "integrations")])


if __name__ == "__main__":
    unittest.main()
