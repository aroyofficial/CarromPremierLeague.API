"""WebAuthn passkeys: security keys, phone or laptop authenticators, and password managers such as Google Password Manager."""

import json
from urllib.parse import urlsplit

from fastapi import HTTPException
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from core.config import settings

RP_NAME = "Carrom Premier League"


def _origin_of(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def allowed_origins() -> set[str]:
    origins = {_origin_of(host) for host in settings.APP_ALLOWED_HOSTS}
    origins.add(_origin_of(settings.PUBLIC_WEB_URL))
    origins.discard("")
    return origins


def relying_party(origin: str | None) -> tuple[str, str]:
    """Return (rp_id, origin) for a browser origin the API trusts."""
    normalized = _origin_of(origin or "")
    if not normalized or normalized not in allowed_origins():
        raise HTTPException(status_code=400, detail="Passkeys work only from the league website.")
    return urlsplit(normalized).hostname, normalized


def _descriptors(credential_ids: list[str]) -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(value)) for value in credential_ids]


def registration_options(origin: str | None, admin, existing_ids: list[str]) -> tuple[dict, str]:
    rp_id, _ = relying_party(origin)
    display = " ".join(part for part in (admin.first_name, admin.last_name) if part) or admin.username
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=str(admin.id).encode(),
        user_name=admin.username,
        user_display_name=display,
        exclude_credentials=_descriptors(existing_ids),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return json.loads(options_to_json(options)), bytes_to_base64url(options.challenge)


def verify_registration(origin: str | None, credential: dict, challenge: str) -> tuple[str, str, int, str | None]:
    rp_id, expected_origin = relying_party(origin)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
        )
    except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as error:
        raise HTTPException(status_code=400, detail="That passkey could not be verified. Try again.") from error
    transports = (credential.get("response") or {}).get("transports") or []
    known = [value for value in transports if value in {item.value for item in AuthenticatorTransport}]
    return (
        bytes_to_base64url(verified.credential_id),
        bytes_to_base64url(verified.credential_public_key),
        verified.sign_count,
        ",".join(known) or None,
    )


def authentication_options(origin: str | None, credential_ids: list[str]) -> tuple[dict, str]:
    rp_id, _ = relying_party(origin)
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=_descriptors(credential_ids),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return json.loads(options_to_json(options)), bytes_to_base64url(options.challenge)


def verify_authentication(origin: str | None, credential: dict, challenge: str, public_key: str, sign_count: int) -> int:
    rp_id, expected_origin = relying_party(origin)
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
            credential_public_key=base64url_to_bytes(public_key),
            credential_current_sign_count=sign_count,
        )
    except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError) as error:
        raise HTTPException(status_code=400, detail="That passkey could not be verified. Try again.") from error
    return verified.new_sign_count
