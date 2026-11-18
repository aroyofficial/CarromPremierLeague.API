"""Web Push delivery. The browser push service is called only from the worker."""

from __future__ import annotations

import json
import logging

from services.notify.copy import alert_body, alert_title
from services.notify.events import SCHEDULE
from services.notify.ics import sequence_for

logger = logging.getLogger(__name__)


def push_payload(event: str, match: dict, before: dict | None, public_web_url: str, notice_id: int | None = None) -> dict:
    season_id = match.get("season_id")
    season_wide = event == SCHEDULE
    anchor = f"#match-{match.get('id')}" if match.get("id") and not season_wide else ""
    url = f"{public_web_url}/schedule{anchor}" if public_web_url else f"/schedule{anchor}"
    return {
        "title": alert_title(event),
        "body": alert_body(event, match, before),
        "tag": f"season-{season_id}-schedule" if season_wide else f"match-{match.get('id')}-{event}",
        "url": url,
        "matchId": None if season_wide else match.get("id"),
        "seasonId": season_id,
        "event": event,
        "noticeId": notice_id,
        "version": sequence_for(match),
    }


def send_web_push(subscription: dict, payload: dict, *, private_key: str, subject: str) -> dict:
    endpoint = str(subscription.get("endpoint") or "")
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return {"ok": False, "blocked": True, "gone": False, "reason": "pywebpush is not installed."}
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {
                    "p256dh": subscription.get("p256dh") or "",
                    "auth": subscription.get("auth") or "",
                },
            },
            data=json.dumps(payload),
            vapid_private_key=private_key,
            vapid_claims={"sub": subject},
            ttl=60 * 60 * 12,
        )
        return {"ok": True, "blocked": False, "gone": False, "reason": ""}
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        gone = status in (404, 410)
        logger.info("push delivery failed status=%s gone=%s", status, gone)
        return {
            "ok": False,
            "blocked": False,
            "gone": gone,
            "reason": f"push service returned {status}" if status else "push service rejected the message",
        }
    except Exception as exc:
        # The type only: a network error's message carries the endpoint URL.
        logger.info("push delivery failed error=%s", type(exc).__name__)
        return {"ok": False, "blocked": False, "gone": False, "reason": "push delivery failed"}
