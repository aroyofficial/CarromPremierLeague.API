import asyncio
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import test_auth_service as fakes
from core.permissions import ALL_CODES, ROLES, USE_ASSISTANT
from core.security import create_access_token, decode_access_token, hash_password
from schemas.auth_schema import AdminCreateRequest, AdminPermissionsUpdate, LoginRequest
from schemas.settings_schema import CompetitionSettings
from services.auth_service import AuthService
from services.settings_service import SettingsService

PASSWORD = "correct-horse-battery"


def run(coro):
    return asyncio.run(coro)


def rules(**extra):
    return {"regulation_minutes": 15, "extra_minutes": 5, "league_queen": False, "queen_needs_cover": False, **extra}


class SwitchTests(unittest.TestCase):
    def test_the_two_switches_are_independent(self):
        settings = CompetitionSettings(**rules(assistant_public_enabled=True, assistant_admin_enabled=False))
        self.assertTrue(settings.assistant_public_enabled)
        self.assertFalse(settings.assistant_admin_enabled)

    def test_a_client_that_sends_nothing_leaves_both_on(self):
        settings = CompetitionSettings(**rules())
        self.assertTrue(settings.assistant_public_enabled)
        self.assertTrue(settings.assistant_admin_enabled)

    def test_the_old_single_value_sets_both_when_the_new_ones_are_missing(self):
        settings = CompetitionSettings(**rules(assistant_enabled=False))
        self.assertFalse(settings.assistant_public_enabled)
        self.assertFalse(settings.assistant_admin_enabled)
        self.assertFalse(settings.assistant_enabled)

    def test_a_new_switch_wins_over_the_old_value(self):
        settings = CompetitionSettings(**rules(assistant_enabled=False, assistant_admin_enabled=True))
        self.assertFalse(settings.assistant_public_enabled)
        self.assertTrue(settings.assistant_admin_enabled)

    def test_the_old_value_reads_on_while_either_switch_is_on(self):
        only_admin = CompetitionSettings(**rules(assistant_public_enabled=False, assistant_admin_enabled=True))
        both_off = CompetitionSettings(**rules(assistant_public_enabled=False, assistant_admin_enabled=False))
        self.assertTrue(only_admin.assistant_enabled)
        self.assertFalse(both_off.assistant_enabled)

    def test_the_response_carries_all_three_values(self):
        data = CompetitionSettings(**rules(assistant_public_enabled=False)).model_dump()
        self.assertEqual(
            {key: data[key] for key in ("assistant_public_enabled", "assistant_admin_enabled", "assistant_enabled")},
            {"assistant_public_enabled": False, "assistant_admin_enabled": True, "assistant_enabled": True},
        )

    def test_update_stores_both_switches(self):
        saved = {}

        class Repository:
            def save(self, settings):
                saved["settings"] = settings
                return settings

        result = SettingsService(Repository()).update(
            CompetitionSettings(**rules(assistant_public_enabled=False, assistant_admin_enabled=True))
        )
        self.assertFalse(result.assistant_public_enabled)
        self.assertTrue(saved["settings"].assistant_admin_enabled)

    def test_an_old_client_update_turns_both_off(self):
        class Repository:
            def save(self, settings):
                return settings

        result = SettingsService(Repository()).update(CompetitionSettings(**rules(assistant_enabled=False)))
        self.assertFalse(result.assistant_public_enabled)
        self.assertFalse(result.assistant_admin_enabled)


class ModeTests(unittest.TestCase):
    @staticmethod
    def _app(data):
        app = FastAPI()

        @app.get("/settings")
        def read():
            return {"success": True, "message": None, "data": data}

        return app

    def test_each_audience_reads_its_own_switch(self):
        from routes.assistant_routes import assistant_is_enabled

        app = self._app({"assistant_public_enabled": False, "assistant_admin_enabled": True, "assistant_enabled": True})
        self.assertFalse(run(assistant_is_enabled(app, "public")))
        self.assertTrue(run(assistant_is_enabled(app, "admin")))

        other = self._app({"assistant_public_enabled": True, "assistant_admin_enabled": False, "assistant_enabled": True})
        self.assertTrue(run(assistant_is_enabled(other, "public")))
        self.assertFalse(run(assistant_is_enabled(other, "admin")))

    def test_a_server_that_only_knows_the_old_value_still_works(self):
        from routes.assistant_routes import assistant_is_enabled

        app = self._app({"assistant_enabled": True})
        self.assertTrue(run(assistant_is_enabled(app, "public")))
        self.assertTrue(run(assistant_is_enabled(app, "admin")))

    def test_unreadable_settings_mean_off(self):
        from routes.assistant_routes import assistant_is_enabled

        self.assertFalse(run(assistant_is_enabled(FastAPI(), "admin")))
        self.assertFalse(run(assistant_is_enabled(self._app({}), "public")))


class CatalogTests(unittest.TestCase):
    def test_the_permission_is_in_the_catalog_and_has_a_role(self):
        self.assertIn(USE_ASSISTANT, ALL_CODES)
        roles = {role.code: role for role in ROLES}
        self.assertEqual(roles["assistant_user"].permissions, (USE_ASSISTANT,))

    def test_the_sql_files_seed_the_same_permission(self):
        import pathlib

        base = pathlib.Path(__file__).resolve().parents[2] / "CarromPremierLeague.Database"
        for name in ("setup.sql", "Setup-Database.ps1"):
            path = base / name
            if not path.exists():
                self.skipTest("database repository is not next to the API")
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("'assistant.use'", text, name)
            self.assertIn("AssistantPublicEnabled", text, name)
            self.assertIn("AssistantAdminEnabled", text, name)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.repository = fakes.FakeAdminRepository()
        self.permissions = fakes.FakePermissionRepository(self.repository)
        self.service = AuthService(self.repository, self.permissions)
        self.manager = self.service.bootstrap_admin(AdminCreateRequest(username="manager", password=PASSWORD))
        password_hash, salt = hash_password(PASSWORD)
        self.member = self.repository.create("member", password_hash, salt, fakes.PASSWORD_ITERATIONS)
        self.permissions.change(self.member.id, {"matches.update"}, set(), self.manager.id)

    def _login(self, username):
        return self.service.login(LoginRequest(username=username, password=PASSWORD)).access_token

    def _grant(self, codes):
        self.service.set_permissions(self.member.id, AdminPermissionsUpdate(permissions=codes), self.manager)

    def test_the_claim_is_issued_at_login(self):
        self.assertIs(decode_access_token(self._login("manager"))["ast"], True)
        self.assertIs(decode_access_token(self._login("member"))["ast"], False)

    def test_the_claim_is_issued_when_a_sign_in_is_approved(self):
        from services.admin_sessions import MemorySessionStore

        service = AuthService(self.repository, self.permissions, sessions=MemorySessionStore())
        first = service.login(LoginRequest(username="manager", password=PASSWORD, device_id="a" * 8))
        second = service.login(LoginRequest(username="manager", password=PASSWORD, device_id="b" * 8))
        self.assertTrue(first.access_token)
        self.assertTrue(second.pending)
        admin = service.current_admin(first.access_token)
        service.approve_login(admin, second.request_id)
        claimed = service.claim_login(second.request_id)
        self.assertIs(decode_access_token(claimed.access_token)["ast"], True)

    def test_refresh_picks_up_a_grant_made_after_login(self):
        token = self._login("member")
        self.assertIs(decode_access_token(token)["ast"], False)
        self._grant(["matches.update", USE_ASSISTANT])
        admin = self.service.current_admin(token)
        fresh = self.service.refresh(admin, token)
        self.assertIs(decode_access_token(fresh.access_token)["ast"], True)
        self.assertEqual(self.service.current_admin(fresh.access_token).id, self.member.id)

    def test_refresh_drops_a_claim_that_was_revoked(self):
        self._grant(["matches.update", USE_ASSISTANT])
        token = self._login("member")
        self.assertIs(decode_access_token(token)["ast"], True)
        self._grant(["matches.update"])
        fresh = self.service.refresh(self.service.current_admin(token), token)
        self.assertIs(decode_access_token(fresh.access_token)["ast"], False)

    def test_refresh_does_not_move_the_expiry(self):
        token = self._login("manager")
        fresh = self.service.refresh(self.service.current_admin(token), token)
        self.assertEqual(decode_access_token(fresh.access_token)["exp"], decode_access_token(token)["exp"])

    def test_refresh_does_not_revive_a_revoked_token(self):
        token = self._login("manager")
        self.repository.update(self.manager.id, {"increment_token_version": True})
        with self.assertRaises(Exception):
            self.service.current_admin(token)


class RouteAccessTests(unittest.TestCase):
    def setUp(self):
        from main import app
        from routes.auth_routes import get_service

        self.app = app
        self.repository = fakes.FakeAdminRepository()
        self.permissions = fakes.FakePermissionRepository(self.repository)
        self.service = AuthService(self.repository, self.permissions)
        self.manager = self.service.bootstrap_admin(AdminCreateRequest(username="manager", password=PASSWORD))
        password_hash, salt = hash_password(PASSWORD)
        self.member = self.repository.create("member", password_hash, salt, fakes.PASSWORD_ITERATIONS)
        self.permissions.change(self.member.id, {"matches.update"}, set(), self.manager.id)
        app.dependency_overrides[get_service] = lambda: self.service
        self.addCleanup(app.dependency_overrides.pop, get_service, None)
        self.client = TestClient(app)

    def _headers(self, username, assistant_claim=None):
        if assistant_claim is None:
            token = self.service.login(LoginRequest(username=username, password=PASSWORD)).access_token
        else:
            record = self.repository.get_credentials_by_username(username)
            token = create_access_token(record["Id"], username, record["TokenVersion"], assistant_claim)
        return {"Authorization": f"Bearer {token}"}

    def _chat(self, headers, path="/assistant/admin/chat", body=None):
        from routes import assistant_routes

        enabled = mock.AsyncMock(return_value=False)
        with mock.patch.object(assistant_routes, "assistant_is_enabled", new=enabled):
            response = self.client.post(path, json=body or {"message": "hi"}, headers=headers)
        return response, enabled

    def test_an_admin_without_access_is_refused_chat_and_confirm(self):
        headers = self._headers("member")
        chat, _ = self._chat(headers)
        confirm, _ = self._chat(headers, "/assistant/admin/confirm", {"token": "x" * 20, "approve": True})
        self.assertEqual(chat.status_code, 403)
        self.assertIn("Use Striker", chat.text)
        self.assertEqual(confirm.status_code, 403)

    def test_an_admin_with_access_reaches_the_admin_switch(self):
        self.service.set_permissions(
            self.member.id, AdminPermissionsUpdate(permissions=["matches.update", USE_ASSISTANT]), self.manager
        )
        response, enabled = self._chat(self._headers("member"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("turned off", response.json()["data"]["reply"])
        enabled.assert_awaited_once()
        self.assertEqual(enabled.await_args.args[1], "admin")

    def test_a_revoked_admin_loses_access_even_with_a_stale_claim(self):
        headers = self._headers("member", assistant_claim=True)
        response, _ = self._chat(headers)
        self.assertEqual(response.status_code, 403)

    def test_a_new_grant_works_before_the_token_is_refreshed(self):
        headers = self._headers("member", assistant_claim=False)
        self.service.set_permissions(
            self.member.id, AdminPermissionsUpdate(permissions=["matches.update", USE_ASSISTANT]), self.manager
        )
        response, _ = self._chat(headers)
        self.assertEqual(response.status_code, 200)

    def test_public_chat_never_needs_the_permission(self):
        from routes import assistant_routes

        enabled = mock.AsyncMock(return_value=False)
        with mock.patch.object(assistant_routes, "assistant_is_enabled", new=enabled):
            response = self.client.post("/assistant/public/chat", json={"message": "hi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(enabled.await_args.args[1], "public")

    def test_refresh_route_returns_a_token_with_current_claims(self):
        headers = self._headers("manager")
        response = self.client.post("/auth/refresh", headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIs(decode_access_token(data["access_token"])["ast"], True)

    def test_refresh_route_needs_sign_in(self):
        self.assertEqual(self.client.post("/auth/refresh").status_code, 401)


if __name__ == "__main__":
    unittest.main()
