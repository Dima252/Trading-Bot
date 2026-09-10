"""Strip credentials out of anything that gets persisted or published.

A failing job stores its traceback in `runs.detail`, and the GitHub Actions
workflow commits that database to a public repository. GitHub masks registered
secrets in workflow *logs* automatically; it does nothing for a file the
workflow commits. So the scrubbing has to happen before the write.

`traceback.format_exc()` renders frames and the exception message, not local
variable values, so a key sitting in a local is not printed. The exposure is
narrower and real: an exception whose *message* quotes a request, a URL with
credentials in the query string, a library that helpfully includes headers.
None of that is likely with this broker. It is also cheap to make impossible.
"""

from __future__ import annotations

import os
import re

# Read from the environment at call time rather than import time, so a key set
# after this module loads is still caught.
SECRET_ENV = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ANTHROPIC_API_KEY")

# Shapes worth removing even when this process never held the value -- a
# credential belonging to something else in the traceback is still a
# credential.
PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bPK[A-Z0-9]{10,}\b"),                  # Alpaca key id
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),            # sk-style tokens
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password)"
        r"\s*[=:]\s*['\"]?([A-Za-z0-9._~+/-]{12,})['\"]?"
    ),
)

PLACEHOLDER = "[redacted]"


def redact(text: str) -> str:
    """Return `text` with anything credential-shaped removed.

    Exact values from the environment go first: those are the only certain
    matches, and replacing them catches a key however it reached the string.
    The patterns then cover credentials this process never held.
    """
    if not text:
        return text

    for name in SECRET_ENV:
        value = os.environ.get(name)
        # A short or empty value would match far too much. Eight characters is
        # below any real key and above anything that could be a placeholder.
        if value and len(value) >= 8:
            text = text.replace(value, f"[{name}]")

    for pattern in PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(rf"\1={PLACEHOLDER}", text)
        else:
            text = pattern.sub(PLACEHOLDER, text)

    return text
