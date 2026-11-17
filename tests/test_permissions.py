import unittest

from tests import test_auth_service as fakes  # noqa: F401  (sets the environment before app imports)

from fastapi import HTTPException
from fastapi.routing import APIRoute

from core.permissions import ALL_CODES, CATALOG, MANAGE_PERMISSIONS, ensure_known
from schemas.auth_schema import (
    AdminCreateRequest,
    AdminPermissionsUpdate,
    AdminResponse,
    AdminUpdateRequest,
)
from services.auth_service import AuthService


def _service():
    admins = fakes.FakeAdminRepository()
    permissions = fakes.FakePermissionRepository(admins)
    return AuthService(admins, permissions)


class CatalogTests(unittest.TestCase):
    def test_codes_are_unique_and_area_scoped(self):
        codes = [permission.code for permission in CATALOG]
        self.assertEqual(len(codes), len(set(codes)))
        for permission in CATALOG:
            self.assertRegex(permission.code, r"^[a-z]+\.[a-z]+$")
            self.assertTrue(permission.name and permission.description and permission.area)

    def test_unknown_code_is_rejected(self):
        with self.assertRaises(ValueError):
            ensure_known("teams.fly")

    @staticmethod
    def _api_routes():
        from main import app
        from routes.system_routes import _iter_routes

        return [route for route in _iter_routes(app) if isinstance(route, APIRoute)]

    def test_every_route_permission_is_in_the_catalog(self):
        used = set()
        for route in self._api_routes():
            for dependency in route.dependant.dependencies:
                used.update(getattr(dependency.call, "permission_codes", ()))
        self.assertGreaterEqual(len(used), 20)
        self.assertLessEqual(used, ALL_CODES)

    def test_every_write_route_names_a_permission_or_requires_sign_in(self):
        open_writes = {
            ("POST", "/auth/login"),
            ("POST", "/auth/login-request/claim"),
            ("POST", "/auth/bootstrap"),
            ("POST", "/auth/forgot-password"),
            ("POST", "/auth/reset-password"),
            ("POST", "/auth/reset-code/verify"),
            ("POST", "/auth/mfa/email-code"),
            ("POST", "/auth/mfa/passkey-options"),
            ("POST", "/auth/mfa/verify"),
            ("POST", "/assistant/public/chat"),
        }
        routes = self._api_routes()
        self.assertGreater(len(routes), 40)
        for route in routes:
            methods = route.methods - {"GET", "HEAD", "OPTIONS"}
            if not methods:
                continue
            calls = [dependency.call for dependency in route.dependant.dependencies]
            guarded = any(
                getattr(call, "permission_codes", None) or getattr(call, "__name__", "") == "get_current_admin"
                for call in calls
            )
            for method in methods:
                if (method, route.path) in open_writes:
                    continue
                self.assertTrue(guarded, f"{method} {route.path} has no permission check")


class RequirePermissionTests(unittest.TestCase):
    @staticmethod
    def _admin(*codes):
        return AdminResponse(id=1, username="someone", is_active=True, permissions=list(codes))

    def test_missing_permission_is_forbidden(self):
        from routes.auth_routes import require_permission

        with self.assertRaises(HTTPException) as raised:
            require_permission("teams.create")(admin=self._admin("players.create"))
        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("Create teams", raised.exception.detail)

    def test_held_permission_passes(self):
        from routes.auth_routes import require_permission

        admin = self._admin("teams.create")
        self.assertIs(require_permission("teams.create")(admin=admin), admin)

    def test_any_listed_permission_passes(self):
        from routes.auth_routes import require_permission

        admin = self._admin(MANAGE_PERMISSIONS)
        self.assertIs(require_permission("admins.view", MANAGE_PERMISSIONS)(admin=admin), admin)

    def test_dependency_resolves_the_signed_in_admin(self):
        from fastapi.params import Depends as DependsParam
        import inspect

        from routes.auth_routes import get_current_admin, require_permission

        default = inspect.signature(require_permission("teams.create")).parameters["admin"].default
        self.assertIsInstance(default, DependsParam)
        self.assertIs(default.dependency, get_current_admin)

    def test_unknown_code_fails_at_declaration(self):
        from routes.auth_routes import require_permission

        with self.assertRaises(ValueError):
            require_permission("teams.fly")


class AdminPermissionRuleTests(unittest.TestCase):
    def setUp(self):
        self.service = _service()
        self.owner = self.service.bootstrap_admin(
            AdminCreateRequest(username="owner", password="correct-horse-battery")
        )

    def _activate(self, admin):
        welcome = next(mail for mail in reversed(self.service.mailer.sent) if f"Username: {admin.username}" in mail.text)
        password = welcome.text.split("Temporary password: ", 1)[1].splitlines()[0]
        token = self.service.login(fakes.LoginRequest(username=admin.username, password=password))
        current = self.service.current_admin(token.access_token)
        self.service.request_password_code(current)
        code = self.service.mailer.sent[-1].text.split("Code: ", 1)[1].splitlines()[0]
        return self.service.update_admin(
            admin.id,
            AdminUpdateRequest(password="replacement-password-1", password_code=code),
            current,
        )

    def _make(self, username, codes=()):
        self._phones = getattr(self, "_phones", 9000000100) + 1
        created = self.service.create_admin(
            AdminCreateRequest(
                username=username,
                password="correct-horse-battery",
                permissions=list(codes),
                email=f"{username}@example.com",
                phone_code="+91",
                phone_number=str(self._phones),
            ),
            self.owner,
        )
        return created

    def test_bootstrap_admin_holds_every_permission(self):
        self.assertEqual(set(self.owner.permissions), ALL_CODES)

    def test_current_admin_carries_permissions(self):
        token = self.service.login(fakes.LoginRequest(username="owner", password="correct-horse-battery"))
        me = self.service.current_admin(token.access_token)
        self.assertEqual(set(me.permissions), ALL_CODES)

    def test_create_with_initial_permissions(self):
        scorer = self._make("scorer", ["matches.update", "matches.scorecard"])
        self.assertEqual(scorer.permissions, ["matches.update", "matches.scorecard"])

    def test_create_with_permissions_needs_manage(self):
        creator = self._make("creator", ["admins.create", "teams.create"])
        with self.assertRaises(HTTPException) as raised:
            self.service.create_admin(
                AdminCreateRequest(username="other", password="correct-horse-battery", permissions=["teams.create"]),
                creator,
            )
        self.assertEqual(raised.exception.status_code, 403)
        plain = self.service.create_admin(
            AdminCreateRequest(
                username="plain",
                password="correct-horse-battery",
                email="plain@example.com",
                phone_code="+91",
                phone_number="9000000002",
            ),
            creator,
        )
        self.assertEqual(plain.permissions, [])

    def test_create_cannot_grant_unheld_permission(self):
        manager = self._make("manager", ["admins.create", MANAGE_PERMISSIONS, "teams.create"])
        with self.assertRaises(HTTPException) as raised:
            self.service.create_admin(
                AdminCreateRequest(username="other", password="correct-horse-battery", permissions=["seasons.delete"]),
                manager,
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_unknown_code_is_a_bad_request(self):
        target = self._make("target")
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(target.id, AdminPermissionsUpdate(permissions=["teams.fly"]), self.owner)
        self.assertEqual(raised.exception.status_code, 400)

    def test_set_permissions_replaces_the_set(self):
        target = self._make("target", ["teams.create"])
        updated = self.service.set_permissions(
            target.id, AdminPermissionsUpdate(permissions=["players.create", "players.update"]), self.owner
        )
        self.assertEqual(updated.permissions, ["players.create", "players.update"])

    def test_cannot_change_permissions_you_do_not_hold(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        target = self._make("target", ["seasons.create"])
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(target.id, AdminPermissionsUpdate(permissions=[]), manager)
        self.assertEqual(raised.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            self.service.set_permissions(
                target.id, AdminPermissionsUpdate(permissions=["seasons.create", "players.create"]), manager
            )
        kept = self.service.set_permissions(
            target.id, AdminPermissionsUpdate(permissions=["seasons.create", "teams.create"]), manager
        )
        self.assertEqual(kept.permissions, ["seasons.create", "teams.create"])

    def test_cannot_remove_own_manage_permission(self):
        self._make("backup", [MANAGE_PERMISSIONS])
        remaining = [code for code in self.owner.permissions if code != MANAGE_PERMISSIONS]
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(self.owner.id, AdminPermissionsUpdate(permissions=remaining), self.owner)
        self.assertEqual(raised.exception.status_code, 400)

    def test_last_manager_cannot_lose_manage_or_be_disabled_or_deleted(self):
        manager = self._activate(self._make("manager", [MANAGE_PERMISSIONS]))
        self.service.set_permissions(
            self.owner.id,
            AdminPermissionsUpdate(permissions=[c for c in self.owner.permissions if c != MANAGE_PERMISSIONS]),
            manager,
        )
        owner = self.service.get_admin(self.owner.id)
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(manager.id, AdminPermissionsUpdate(permissions=[]), manager)
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(HTTPException) as raised:
            self.service.update_admin(manager.id, AdminUpdateRequest(is_active=False), owner)
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(HTTPException) as raised:
            self.service.delete_admin(manager.id, owner)
        self.assertEqual(raised.exception.status_code, 400)

    def test_another_manager_allows_disable_and_delete(self):
        manager = self._activate(self._make("manager", [MANAGE_PERMISSIONS]))
        disabled = self.service.update_admin(manager.id, AdminUpdateRequest(is_active=False), self.owner)
        self.assertFalse(disabled.is_active)
        self.assertTrue(self.service.delete_admin(manager.id, self.owner))

    def test_self_service_profile_change_without_admins_update(self):
        member = self._activate(self._make("member", ["teams.create"]))
        updated = self.service.update_admin(member.id, AdminUpdateRequest(first_name="Member"), member)
        self.assertEqual(updated.first_name, "Member")
        self.assertEqual(updated.username, member.username)
        with self.assertRaises(HTTPException) as raised:
            self.service.update_admin(member.id, AdminUpdateRequest(username="member2"), member)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Username cannot be changed.")
        with self.assertRaises(HTTPException) as raised:
            self.service.update_admin(self.owner.id, AdminUpdateRequest(username="taken-over"), member)
        self.assertEqual(raised.exception.status_code, 403)

    def test_admin_list_includes_permissions(self):
        self._make("scorer", ["matches.update"])
        listed = {admin.username: admin.permissions for admin in self.service.list_admins()}
        self.assertEqual(listed["scorer"], ["matches.update"])
        self.assertEqual(set(listed["owner"]), ALL_CODES)


class DelegationGuardTests(unittest.TestCase):
    """Delegation can only pass on permissions the caller holds right now, whatever the payload says."""

    def setUp(self):
        self.service = _service()
        self.owner = self.service.bootstrap_admin(AdminCreateRequest(username="owner", password="correct-horse-battery"))
        self._phones = 9200000000

    def _make(self, username, codes=()):
        self._phones += 1
        return self.service.create_admin(
            AdminCreateRequest(
                username=username,
                password="correct-horse-battery",
                permissions=list(codes),
                email=f"{username}@example.com",
                phone_code="+91",
                phone_number=str(self._phones),
            ),
            self.owner,
        )

    def _admin_count(self):
        return len(self.service.list_admins())

    def test_a_revoked_permission_cannot_be_passed_on_with_a_fresh_actor(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        self.service.set_permissions(manager.id, AdminPermissionsUpdate(permissions=[MANAGE_PERMISSIONS]), self.owner)
        # Every request loads the actor from the database, so the revoked permission is gone.
        fresh = self.service.get_admin(manager.id)
        self.assertNotIn("teams.create", fresh.permissions)
        before = self._admin_count()
        with self.assertRaises(HTTPException) as raised:
            self.service.create_admin(
                AdminCreateRequest(username="late", password="correct-horse-battery", permissions=["teams.create"]),
                fresh,
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self._admin_count(), before)

    def test_a_revoked_permission_cannot_be_granted_on_update_either(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        target = self._make("target")
        self.service.set_permissions(manager.id, AdminPermissionsUpdate(permissions=[MANAGE_PERMISSIONS]), self.owner)
        fresh = self.service.get_admin(manager.id)
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(target.id, AdminPermissionsUpdate(permissions=["teams.create"]), fresh)
        self.assertEqual(raised.exception.status_code, 403)

    def test_a_manager_cannot_grant_itself_a_permission_it_lacks(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS])
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(
                manager.id,
                AdminPermissionsUpdate(permissions=[MANAGE_PERMISSIONS, "seasons.delete"]),
                manager,
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertNotIn("seasons.delete", self.service.get_admin(manager.id).permissions)

    def test_one_unheld_code_rejects_the_whole_grant(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        before = self._admin_count()
        with self.assertRaises(HTTPException) as raised:
            self.service.create_admin(
                AdminCreateRequest(
                    username="partial",
                    password="correct-horse-battery",
                    permissions=["teams.create", "seasons.delete"],
                ),
                manager,
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self._admin_count(), before)

    def test_the_refusal_does_not_reveal_what_the_caller_holds(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        with self.assertRaises(HTTPException) as raised:
            self.service.create_admin(
                AdminCreateRequest(username="nosy", password="correct-horse-battery", permissions=["seasons.delete"]),
                manager,
            )
        detail = str(raised.exception.detail)
        self.assertEqual(detail, "You can only grant permissions you already hold.")
        self.assertNotIn("teams.create", detail)
        self.assertNotIn("seasons.delete", detail)

    def test_tampered_codes_are_rejected_as_unknown(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        for code in ("TEAMS.CREATE", " teams.create", "teams.create ", "teams.create;drop", "", "*"):
            with self.subTest(code=code):
                with self.assertRaises(HTTPException) as raised:
                    self.service.create_admin(
                        AdminCreateRequest(username="tamper", password="correct-horse-battery", permissions=[code]),
                        manager,
                    )
                self.assertIn(raised.exception.status_code, (400, 403))

    def test_holding_the_permission_lets_you_delegate_it(self):
        manager = self._make("manager", [MANAGE_PERMISSIONS, "teams.create"])
        created = self.service.create_admin(
            AdminCreateRequest(
                username="delegate",
                password="correct-horse-battery",
                email="delegate@example.com",
                phone_code="+91",
                phone_number="9200000900",
                permissions=["teams.create"],
            ),
            manager,
        )
        self.assertEqual(created.permissions, ["teams.create"])


if __name__ == "__main__":
    unittest.main()
