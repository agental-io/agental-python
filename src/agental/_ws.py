"""The WebSocket a sandbox's terminal is opened with, in both clients, following no redirect.

A redirect would carry the subprotocols, and the ticket in them, to wherever it points, another
host included: websockets drops only Authorization and cookies on the way. So the client that
awaits refuses every redirect, and the client that waits follows none in the first place.
"""

from __future__ import annotations

from websockets.asyncio.client import connect as _AsyncConnect
from websockets.sync.client import connect as connect


class aconnect(_AsyncConnect):  # noqa: N801 - named as the function it stands in for
    """websockets' connect for asyncio, with every redirect refused."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


__all__ = ["aconnect", "connect"]
