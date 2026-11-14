import unittest

from tests import test_auth_service as fakes  # noqa: F401  (sets the environment before app imports)

from fastapi import HTTPException

from core.permissions import (
    ALL_CODES,
    AREAS,
    CATALOG,
    MANAGE_PERMISSIONS,
    ROLES,
    SECTIONS,
    children_of,
    missing_parents,
    parent_of,
)
from schemas.auth_schema import AdminCreateRequest, AdminPermissionsUpdate
from services.auth_service import AuthService


def _service():
    admins = fakes.FakeAdminRepository()
    return AuthService(admins, fakes.FakePermissionRepository(admins))


class HierarchyCatalogTests(unittest.TestCase):
    def test_every_permission_sits_in_a_known_area_and_section(self):
        area_codes = {area.code for area in AREAS}
        section_codes = {section.code for section in SECTIONS}
        self.assertEqual(len(area_codes), len(AREAS))
        self.assertEqual(len(section_codes), len(SECTIONS))
        for area in AREAS:
            self.assertIn(area.section, section_codes)
            self.assertTrue(area.name and area.description and area.icon)
        for permission in CATALOG:
            self.assertIn(permission.area, area_codes)

    def test_parents_are_roots_in_the_same_area(self):
        by_code = {permission.code: permission for permission in CATALOG}
        for permission in CATALOG:
            if permission.parent is None:
                continue
            parent = by_code[permission.parent]
            self.assertEqual(parent.area, permission.area, permission.code)
            self.assertIsNone(parent.parent, f"{permission.code} has a nested parent")

    def test_parent_comes_before_its_children_in_catalog_order(self):
        order = [permission.code for permission in CATALOG]
        for permission in CATALOG:
            if permission.parent:
                self.assertLess(order.index(permission.parent), order.index(permission.code))

    def test_expected_links(self):
        self.assertEqual(parent_of("matches.scorecard"), "matches.update")
        self.assertEqual(parent_of("admins.delete"), "admins.view")
        self.assertIsNone(parent_of("admins.create"))
        self.assertIsNone(parent_of(MANAGE_PERMISSIONS))
        self.assertEqual(children_of("seasons.update"), ["seasons.delete", "seasons.teams"])

    def test_missing_parents_lists_child_and_parent(self):
        self.assertEqual(missing_parents({"matches.reset", "teams.create"}), {"matches.reset": "matches.update"})
        self.assertEqual(missing_parents({"matches.reset", "matches.update"}), {})

    def test_roles_are_known_closed_sets(self):
        self.assertGreaterEqual(len(ROLES), 3)
        self.assertEqual(len({role.code for role in ROLES}), len(ROLES))
        for role in ROLES:
            self.assertTrue(role.name and role.description)
            self.assertLessEqual(set(role.permissions), ALL_CODES, role.code)
            self.assertEqual(missing_parents(set(role.permissions)), {}, role.code)


class HierarchyRuleTests(unittest.TestCase):
    def setUp(self):
        self.service = _service()
        self.owner = self.service.bootstrap_admin(
            AdminCreateRequest(username="owner", password="correct-horse-battery")
        )
        self.target = self.service.create_admin(
            AdminCreateRequest(username="target", password="correct-horse-battery", permissions=["matches.update"]),
            self.owner,
        )

    def test_save_with_child_but_no_parent_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.set_permissions(
                self.target.id, AdminPermissionsUpdate(permissions=["matches.scorecard"]), self.owner
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Save scorecards", raised.exception.detail)
        self.assertIn("Edit and run matches", raised.exception.detail)

    def test_save_with_parent_and_child_passes(self):
        updated = self.service.set_permissions(
            self.target.id,
            AdminPermissionsUpdate(permissions=["matches.update", "matches.scorecard"]),
            self.owner,
        )
        self.assertEqual(updated.permissions, ["matches.update", "matches.scorecard"])

    def test_create_with_child_but_no_parent_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.create_admin(
                AdminCreateRequest(username="other", password="correct-horse-battery", permissions=["teams.delete"]),
                self.owner,
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_creating_admin_without_view_stays_possible(self):
        created = self.service.create_admin(
            AdminCreateRequest(username="recruiter", password="correct-horse-battery", permissions=["admins.create"]),
            self.owner,
        )
        self.assertEqual(created.permissions, ["admins.create"])


class TreeResponseTests(unittest.TestCase):
    def test_tree_nests_sections_areas_and_permissions(self):
        from routes.auth_routes import build_permission_tree

        tree = build_permission_tree()
        self.assertEqual([section.code for section in tree.sections], [section.code for section in SECTIONS])
        flattened = [
            permission.code
            for section in tree.sections
            for area in section.areas
            for permission in area.permissions
        ]
        self.assertEqual(flattened, [permission.code for permission in CATALOG])
        matches = next(area for section in tree.sections for area in section.areas if area.code == "matches")
        scorecard = next(permission for permission in matches.permissions if permission.code == "matches.scorecard")
        self.assertEqual(scorecard.parent, "matches.update")
        self.assertEqual([role.code for role in tree.roles], [role.code for role in ROLES])

    def test_flat_list_keeps_area_name_and_adds_area_code_and_parent(self):
        from routes.auth_routes import list_permissions

        items = list_permissions(None).data
        reset = next(item for item in items if item.code == "matches.reset")
        self.assertEqual(reset.area, "Matches")
        self.assertEqual(reset.area_code, "matches")
        self.assertEqual(reset.parent, "matches.update")


if __name__ == "__main__":
    unittest.main()
