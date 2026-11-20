"""The one output path: every byte ticketctl prints goes through :func:`write`.

The text is sanitized and scrubbed (``sanitize.render``, body class so a multi-line human listing
keeps its line breaks) before it reaches stdout or stderr, so a configured token never leaves the
process in a result, an error, a warning or a verbose line. A broken or closed stream never changes
the exit code.
"""

from __future__ import annotations

from typing import Any

from .sanitize import render


def write(stream: Any, text: str) -> None:
    safe = render(text, body=True)
    try:
        stream.write(safe + "\n")
        stream.flush()
    except (OSError, ValueError, AttributeError):
        # A full disk, a broken pipe, a closed or missing stream: the exit code still stands.
        pass
