"""Keep calendar feed tokens out of request logs.

The feed URL is the credential: anyone holding it can read the feed until it is
revoked. Access logs record the request path, so the token is replaced there.
"""

from __future__ import annotations

import logging
import re

_FEED_PATH = re.compile(r"(/calendar/feeds/)[^/\s?#]+")
REDACTED = r"\1[redacted]"


def redact_feed_tokens(text: str) -> str:
    return _FEED_PATH.sub(REDACTED, text)


class FeedTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_feed_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_feed_tokens(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact_feed_tokens(value) if isinstance(value, str) else value for key, value in record.args.items()}
        return True


def install_feed_token_filter() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "gunicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, FeedTokenFilter) for existing in logger.filters):
            logger.addFilter(FeedTokenFilter())
