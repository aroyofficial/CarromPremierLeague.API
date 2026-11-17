import unittest

from core.phone_numbers import PhoneNumberError, canonicalize


class CanonicalPhoneTests(unittest.TestCase):
    def test_spaces_trunk_zero_and_repeated_country_prefix_match(self):
        expected = ("+91", "9000000011", "+919000000011")
        self.assertEqual(canonicalize("+91", "90000 00011"), expected)
        self.assertEqual(canonicalize("+91", "09000000011"), expected)
        self.assertEqual(canonicalize("+91", "919000000011"), expected)
        self.assertEqual(canonicalize("0091", "90000-00011"), expected)

    def test_an_indian_mobile_that_starts_with_91_is_kept_whole(self):
        self.assertEqual(canonicalize("+91", "9123456789"), ("+91", "9123456789", "+919123456789"))
        self.assertEqual(canonicalize("+91", "91 9123456789"), ("+91", "9123456789", "+919123456789"))

    def test_a_national_number_that_starts_with_another_country_code_is_kept_whole(self):
        self.assertEqual(canonicalize("+44", "4412345678"), ("+44", "4412345678", "+444412345678"))
        self.assertEqual(canonicalize("+1", "1234567890"), ("+1", "1234567890", "+11234567890"))

    def test_a_repeated_prefix_typed_as_international_is_removed_outside_india(self):
        self.assertEqual(canonicalize("+44", "+44 7700 900123"), ("+44", "7700900123", "+447700900123"))
        self.assertEqual(canonicalize("+44", "0044 7700 900123"), ("+44", "7700900123", "+447700900123"))
        self.assertEqual(canonicalize("+44", "+44 (0)20 7946 0958"), ("+44", "2079460958", "+442079460958"))

    def test_a_short_or_non_numeric_number_is_rejected(self):
        for number in ("900000001", "90000000111", "90000-0001", "abcdefghij"):
            with self.assertRaises(PhoneNumberError):
                canonicalize("+91", number)

    def test_an_empty_number_is_rejected(self):
        with self.assertRaises(PhoneNumberError):
            canonicalize("+91", "   ")
