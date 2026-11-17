import unittest

from core.admin_usernames import username_candidates


class UsernameCandidateTests(unittest.TestCase):
    def test_first_and_last_name_are_the_username(self):
        self.assertEqual(username_candidates("Ada", "Lovelace", "ada@example.com")[0], "ada.lovelace")

    def test_the_same_name_uses_the_email_before_a_number(self):
        candidates = username_candidates("Ada", "Lovelace", "ada.other@example.com")
        self.assertEqual(candidates[0], "ada.lovelace")
        self.assertEqual(candidates[1], "ada.lovelace.ada.other")
        self.assertIn("ada.lovelace2", candidates)

    def test_email_alone_uses_the_mailbox_name(self):
        self.assertEqual(username_candidates(None, None, "New-Admin@Example.com")[0], "new-admin")

    def test_a_single_name_is_joined_to_the_email(self):
        self.assertEqual(username_candidates("New", None, "new-admin@example.com")[0], "new.new-admin")

    def test_accents_and_spaces_are_dropped(self):
        self.assertEqual(username_candidates(" José ", "García", "j@example.com")[0], "jose.garcia")

    def test_a_short_name_joins_the_mailbox(self):
        self.assertEqual(username_candidates("A", None, "ab@example.com")[0], "a.ab")

    def test_nothing_usable_yields_no_candidate(self):
        self.assertEqual(username_candidates("A", None, None), [])
        self.assertEqual(username_candidates(None, None, "ab@example.com"), [])
