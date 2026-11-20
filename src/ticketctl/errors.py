"""Exit-code table, error type and error envelope.

The one table of docs/interface.md (Exit codes) lives here as data: every other
module raises :class:`TicketctlError` with a ``code`` and the table decides the exit
status, the allowed ``reason`` values and the defaults for ``retryable`` and
``outcome_unknown``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import ConfigError
from .gateway import GatewayError
from .sanitize import InputError, cap_text, sanitize_line, scrub
from .state import StateError

MESSAGE_MAX = 4096
TRUNCATED = "<truncated>"

# The keys `details` may carry (interface.md, Error envelope). Anything else is a bug.
DETAILS_KEYS = frozenset(
    {
        "attempt_id",
        "candidates",
        "available_targets",
        "field",
        "fields",
        "option",
        "token",
        "required_fields",
        "plan",
        "result",
        "hook",
        "escalated_hooks",
        "hook_stderr",
        "decision_ids",
        "existing_id",
        "existing_key",
        "current_status",
        "expected_status",
        "reason_hint",
        "field_errors",
        "errors",
        "errorMessages",
        "rate_limit",
        "token_env",
    }
)


@dataclass(frozen=True)
class CodeSpec:
    """One `code` of the table: its exit status and its `reason` rules."""

    exit: int
    reasons: frozenset[str]  # the allowed non-null reasons
    reason_required: bool = False  # reason must be non-null (exits 2, 20, 22)
    reason_is_code: bool = False  # exit 20: the reason equals the code
    retryable: bool = False  # the default; `partial` copies the failing step instead
    allows_retry_after: bool = False


def _spec(exit_: int, *reasons: str, **kw: bool) -> CodeSpec:
    return CodeSpec(exit_, frozenset(reasons), **kw)


_EXIT_20 = ("stale_ticket", "precondition_failed", "no_such_transition")

CODES: dict[str, CodeSpec] = {
    "internal": _spec(1, "audit_failed"),
    "usage": _spec(
        2,
        "human_only",
        "yes_requires_expect_plan",
        "dedupe_key_required",
        "stdin_conflict",
        "invalid_combination",
        "missing_option",
        "invalid_argument",
        reason_required=True,
    ),
    "invalid_config": _spec(
        2, "config_invalid", "no_sites", "account_mismatch", reason_required=True
    ),
    "not_found": _spec(10),
    "vetoed": _spec(11),
    "auth_required": _spec(12),
    "bad_credentials": _spec(12),
    "forbidden": _spec(12),
    "rejected": _spec(12),
    "unavailable": _spec(12, "locked", retryable=True, allows_retry_after=True),
    "timeout": _spec(12, retryable=True),
    "outcome_unknown": _spec(12, retryable=True),
    "rate_limited": _spec(12, retryable=True, allows_retry_after=True),
    "provider_changed": _spec(12),
    "partial": _spec(12, allows_retry_after=True),
    "lookup_incomplete": _spec(12),
    "confirmation_required": _spec(13),
    "escalated": _spec(13),
    **{
        c: _spec(20, c, reason_required=True, reason_is_code=True)
        for c in (*_EXIT_20, "sprint_closed", "already_exists", "declined")
    },
    "ambiguous_site": _spec(21),
    "ambiguous_field": _spec(21),
    "ambiguous_transition": _spec(21),
    "ambiguous_board": _spec(21),
    "ambiguous_sprint": _spec(21),
    "invalid_request": _spec(
        22,
        "invalid_query",
        "unknown_field",
        "not_editable",
        "invalid_value",
        "needs_resolution",
        "missing_required_fields",
        "node_not_allowed",
        "body_too_large",
        "empty_body",
        "unknown_site",
        "invalid_key",
        "mixed_sites",
        "invalid_file",
        "secret_in_body",
        "cursor_invalid",
        "cursor_expired",
        reason_required=True,
    ),
}


def clean_message(text: str) -> str:
    """Make `text` safe for one line of output and cap it.

    Control, format, surrogate, private-use and unassigned characters (and every line break)
    become ``<U+XXXX>`` markers, then the sanitizer's own invisible-character rules and the output
    scrub run, and the result is cut to MESSAGE_MAX (the scrub runs before the cut, so a secret
    cannot straddle it).
    """
    out: list[str] = []
    for ch in text:
        if unicodedata.category(ch)[0] == "C" or ch in "\u2028\u2029":
            out.append(f"<U+{ord(ch):04X}>")
        else:
            out.append(ch)
    cleaned = scrub(sanitize_line("".join(out)))
    return cap_text(cleaned, MESSAGE_MAX)[0]


class TicketctlError(Exception):
    """A failure with a stable `code`; the table maps it to an exit status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        site: str | None = None,
        key: str | None = None,
        retryable: bool | None = None,
        retry_after_s: int | None = None,
        outcome_unknown: bool | None = None,
    ) -> None:
        super().__init__(message)
        spec = CODES.get(code)
        if spec is None:
            raise ValueError(f"unknown error code {code!r}")
        if reason is None and spec.reason_is_code:
            reason = code
        if reason is None:
            if spec.reason_required:
                raise ValueError(f"code {code!r} requires a reason")
        elif reason not in spec.reasons:
            raise ValueError(f"reason {reason!r} is not allowed for code {code!r}")
        if retry_after_s is not None and not spec.allows_retry_after:
            raise ValueError(f"code {code!r} never carries retry_after_s")
        if retry_after_s is not None and retry_after_s < 0:
            raise ValueError("retry_after_s must not be negative")
        self._check_overrides(code, spec, retryable, outcome_unknown)
        if details is not None:
            extra = set(details) - DETAILS_KEYS
            if extra:
                raise ValueError(f"unknown details keys {sorted(extra)}")
        self.code = code
        self.reason = reason
        self.message = message
        self.details = dict(details) if details else None
        self.site = site
        self.key = key
        self.exit = spec.exit
        self.retryable = spec.retryable if retryable is None else retryable
        self.retry_after_s = retry_after_s
        self.outcome_unknown = (
            (code == "outcome_unknown") if outcome_unknown is None else outcome_unknown
        )

    @staticmethod
    def _check_overrides(
        code: str, spec: CodeSpec, retryable: bool | None, outcome_unknown: bool | None
    ) -> None:
        """`retryable` and `outcome_unknown` follow the table (interface.md, Exit codes).

        `partial` copies both from the failing step; `outcome_unknown` may be non-retryable
        (a comment or create without a dedupe key) but is always `outcome_unknown: true`.
        """
        if code != "partial":
            # A write that got a 3xx or an unusable 2xx is `provider_changed` with
            # `outcome_unknown: true` (interface.md, Invoking the provider).
            if (
                outcome_unknown is not None
                and outcome_unknown != (code == "outcome_unknown")
                and not (code == "provider_changed" and outcome_unknown)
            ):
                raise ValueError(f"code {code!r} fixes outcome_unknown")
            if code != "outcome_unknown" and retryable not in (None, spec.retryable):
                raise ValueError(f"code {code!r} fixes retryable")

    def body(self) -> dict[str, Any]:
        """The envelope's `error` object (schemas/cli/error.json)."""
        return {
            "code": self.code,
            "message": clean_message(self.message),
            "reason": self.reason,
            "retryable": self.retryable,
            "retry_after_s": self.retry_after_s,
            "outcome_unknown": self.outcome_unknown,
            "site": self.site,
            "key": self.key,
            "details": self.details,
        }

    def envelope(self) -> dict[str, Any]:
        return {"error": self.body()}


def usage_error(reason: str, message: str, **kw: Any) -> TicketctlError:
    """Exit 2 `usage`."""
    return TicketctlError("usage", message, reason=reason, **kw)


def internal_error(exc: BaseException) -> TicketctlError:
    """Exit 1 `internal`: the exception class and a fixed message, never its text."""
    name = type(exc).__name__
    return TicketctlError(
        "internal",
        f"unexpected internal error ({name}); set TICKETCTL_DEBUG for a traceback",
    )


def from_exception(exc: BaseException) -> TicketctlError | None:
    """Map the modules' own errors onto the envelope; None for any other exception.

    ConfigError (exit 2 `invalid_config`), InputError (the grammar and input refusals, with their
    own exit, code and reason), GatewayError (the HTTP boundary) and StateError (exit 1 `internal`;
    the table's only `internal` reason is `audit_failed`, so the state reason goes in the message).
    """
    if isinstance(exc, TicketctlError):
        return exc
    if isinstance(exc, ConfigError):
        return TicketctlError("invalid_config", str(exc), reason=exc.reason)
    if isinstance(exc, InputError):
        return TicketctlError(exc.code, exc.message, reason=exc.reason, details=exc.details)
    if isinstance(exc, GatewayError):
        details = dict(exc.details)
        reason = details.pop("reason", None)
        return TicketctlError(
            exc.code,
            exc.message,
            reason=reason,
            details=details or None,
            retryable=exc.retryable,
            outcome_unknown=exc.outcome_unknown,
            retry_after_s=exc.retry_after_s,
        )
    if isinstance(exc, StateError):
        return TicketctlError("internal", f"state error ({exc.reason}): {exc.message}")
    return None
