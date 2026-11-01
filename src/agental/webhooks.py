from __future__ import annotations

import hashlib
import hmac
import re


def verify_webhook(body: str | bytes, signature: str | None, secret: str) -> bool:
    """Whether a webhook came from agental.

    Pass the request's body exactly as it arrived, before any JSON parsing, the value of
    its X-Agental-Signature header, and the webhook's signing secret.
    """
    sent = re.fullmatch(r"sha256=([0-9a-f]{64})", (signature or "").strip())
    if not sent:
        return False
    raw = body.encode() if isinstance(body, str) else body
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent.group(1))
