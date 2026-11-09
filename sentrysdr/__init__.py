"""SentrySDR."""
from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = "0.2.0"


def _build_id() -> str:
    """A fingerprint of this copy's code, so a master can tell when a slave runs different code.

    Version numbers are only as good as remembering to bump them; this changes with any edit.
    """
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


BUILD_ID = _build_id()
