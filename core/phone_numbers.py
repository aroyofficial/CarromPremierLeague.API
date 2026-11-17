"""Canonical phone numbers.

A profile stores the country code and the national number separately. Uniqueness
uses the E.164 form so "09000 000011", "+91", and "919000000011" are one number.
"""

import re

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_INDIA_MOBILE = re.compile(r"^[6-9]\d{9}$")


class PhoneNumberError(ValueError):
    pass


class PhoneTaken(Exception):
    pass


def _is_duplicate_key(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("2627", "2601", "primary key", "unique", "duplicate"))


def raise_if_phone_taken(db, exc: Exception) -> None:
    """Turn a duplicate key on a profile's PhoneE164 unique index into PhoneTaken.

    Two requests for the same number on the same table can both pass the check, and the
    loser then fails on UX_tblAdmins_PhoneE164 or UX_tblPlayers_PhoneE164 before its claim
    is written. Other unique keys (such as the username) are left to the caller.
    """
    if "phonee164" in str(exc).lower() and _is_duplicate_key(exc):
        db.rollback()
        raise PhoneTaken() from exc


def claim_phone(db, kind: str, owner_id: int, e164: str | None) -> None:
    """Make this profile the one owner of the number, or release its claim when e164 is None.

    tblContactPhones has the number as its primary key, so two profiles cannot both win,
    even when two requests race past the application-level check. A duplicate rolls the
    open transaction back and raises PhoneTaken.
    """
    cursor = db.cursor()
    cursor.execute(
        "DELETE FROM tblContactPhones WHERE OwnerKind = %s AND OwnerId = %s",
        (kind, owner_id),
    )
    if not e164:
        return
    try:
        cursor.execute(
            "INSERT INTO tblContactPhones (PhoneE164, OwnerKind, OwnerId) VALUES (%s, %s, %s)",
            (e164, kind, owner_id),
        )
    except Exception as exc:
        if _is_duplicate_key(exc):
            db.rollback()
            raise PhoneTaken() from exc
        raise


def canonicalize(phone_code: str, phone_number: str) -> tuple[str, str, str]:
    """Return (country code, national number, E.164).

    Spaces, dashes, a trunk 0, and a repeated country prefix are removed first.
    Indian numbers must be a 10-digit mobile. Other countries must still form a
    valid E.164 number.
    """
    code_digits = re.sub(r"\D", "", phone_code or "")
    national = re.sub(r"\D", "", phone_number or "")
    if not code_digits or not national:
        raise PhoneNumberError("Enter a valid phone number.")
    typed_international = (phone_number or "").lstrip().startswith(("+", "00"))
    code_digits = code_digits.lstrip("0")
    national = national.lstrip("0")
    if code_digits and national.startswith(code_digits):
        remainder = national[len(code_digits):]
        # A national number may itself start with the country code's digits. An Indian mobile is
        # exactly 10 digits, so the prefix goes only when the number is longer. Elsewhere it goes
        # only when it was typed as an international number or the result would be too long.
        if code_digits == "91":
            strip = len(national) > 10
        else:
            strip = typed_international or len(code_digits) + len(national) > 15
        if len(remainder) >= 8 and strip:
            # "+44 (0)20 ..." leaves a trunk 0 after the country code.
            national = remainder.lstrip("0")
    if not code_digits or not national:
        raise PhoneNumberError("Enter a valid phone number.")
    e164 = f"+{code_digits}{national}"
    if not _E164.fullmatch(e164):
        raise PhoneNumberError("Enter a valid phone number.")
    if code_digits == "91" and not _INDIA_MOBILE.fullmatch(national):
        raise PhoneNumberError("Enter a valid Indian mobile number.")
    return f"+{code_digits}", national, e164
