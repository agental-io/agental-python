"""Writes src/agental/_sync.py, the client that waits, from _async.py, the client that awaits.

    python scripts/make_sync.py           writes the file
    python scripts/make_sync.py --check   fails when the file is not what would be written
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "agental"

CHANGES = [
    (r'^"""The client that awaits\..*"""$', '"""The client that waits. Written from _async.py by scripts/make_sync.py: change that file, not this one."""'),
    (r"\bimport asyncio\b", "import time"),
    (r"\basyncio\.sleep\b", "time.sleep"),
    (r"\bAsyncIterator\b", "Iterator"),
    (r"\bhttpx\.AsyncClient\b", "httpx.Client"),
    (r"\basync (def|with|for)\b", r"\1"),
    (r"\bawait ", ""),
    (r"\bwebsockets\.asyncio\.client\b", "websockets.sync.client"),
    (r"__aiter__", "__iter__"),
    (r"\baiter_lines\b", "iter_lines"),
    (r"\baread\b", "read"),
    (r"\baclose\b", "close"),
    (r"\baconnect\b", "connect"),
    (r"__aenter__", "__enter__"),
    (r"__aexit__", "__exit__"),
    (r"\bAsync(?=[A-Z])", ""),
]


def sync_source() -> str:
    source = (PACKAGE / "_async.py").read_text()
    for pattern, replacement in CHANGES:
        source = re.sub(pattern, replacement, source, flags=re.MULTILINE)
    return source


if __name__ == "__main__":
    target = PACKAGE / "_sync.py"
    if "--check" in sys.argv:
        if not target.exists() or target.read_text() != sync_source():
            sys.exit("src/agental/_sync.py is not what _async.py gives. Run: python scripts/make_sync.py")
    else:
        target.write_text(sync_source())
