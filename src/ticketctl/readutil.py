"""Small helpers shared by the read commands ``comments``, ``transitions`` and ``sprints``.

* :func:`object_error` is the envelope of a non-2xx answer of an endpoint that names an object (a
  404 is exit 10 ``not_found``, any other status the gateway did not already raise is exit 12
  ``provider_changed``).
* :func:`hide_field_ids` keeps a provider custom field id out of text that is printed as a name.
* :func:`warn_incomplete` prints the one stderr line that stands for the ``incomplete`` list of a
  command whose schema has no place for it (``comments``, ``transitions``, ``sprints``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .errors import TicketctlError
from .runtime import Runtime

_FIELD_ID = re.compile(r"customfield_[0-9]+", re.IGNORECASE | re.ASCII)


def object_error(status: int, what: str, **details: str) -> TicketctlError:
    """The error of a non-2xx read of an object (the gateway has raised 401, 403, 429, 5xx)."""
    if status == 404:
        return TicketctlError(
            "not_found",
            f"the {what} is missing or you have no permission to see it",
            details=details or None,
        )
    return TicketctlError("provider_changed", f"unexpected answer from the provider ({status})")


def hide_field_ids(text: str) -> str:
    """``text`` with every ``customfield_N`` id replaced (no field id is printed outside fields)."""
    return _FIELD_ID.sub("field", text)


def warn_incomplete(rt: Runtime, what: str, parts: Iterable[str]) -> None:
    names = sorted(set(parts))
    if names:
        rt.warn(f"warning: {what} could not be read completely ({', '.join(names)})")
