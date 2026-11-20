"""The module errors map onto the envelope (issue #11): code, exit, reason, flags, no leaks."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ticketctl import flags, sanitize
from ticketctl.config import ConfigError
from ticketctl.errors import TicketctlError, clean_message, from_exception
from ticketctl.gateway import GatewayError
from ticketctl.sanitize import InputError
from ticketctl.state import StateError

SECRET = "mapping-secret-0123456789"


@pytest.fixture(autouse=True)
def _scrubber() -> Any:
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


def mapped(exc: BaseException) -> TicketctlError:
    err = from_exception(exc)
    assert err is not None
    return err


def test_config_error_maps_with_its_reason() -> None:
    err = mapped(ConfigError("must be a url", key="sites.x.url"))
    assert (err.exit, err.code, err.reason) == (2, "invalid_config", "config_invalid")
    assert "sites.x.url" in err.message
    assert mapped(ConfigError("none", key="sites", reason="no_sites")).reason == "no_sites"


def test_input_error_keeps_its_exit_code_code_and_reason() -> None:
    with pytest.raises(InputError) as exc:
        sanitize.GRAMMARS["ticket_key"].check("nope")
    err = mapped(exc.value)
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "invalid_key")
    with pytest.raises(InputError) as exc:
        sanitize.GRAMMARS["actor"].check("nope")
    err = mapped(exc.value)
    assert (err.exit, err.code, err.reason) == (2, "usage", "invalid_argument")


def test_gateway_error_keeps_flags_and_splits_the_reason_from_details() -> None:
    err = mapped(
        GatewayError(
            "rate_limited", "slow down", retry_after_s=7, details={"rate_limit": {"limit": 1}}
        )
    )
    assert (err.exit, err.code, err.retryable, err.retry_after_s) == (12, "rate_limited", True, 7)
    assert err.details == {"rate_limit": {"limit": 1}}
    err = mapped(
        GatewayError("invalid_config", "bad url", exit_code=2, details={"reason": "config_invalid"})
    )
    assert (err.exit, err.code, err.reason, err.details) == (
        2,
        "invalid_config",
        "config_invalid",
        None,
    )
    err = mapped(GatewayError("auth_required", "unset", details={"token_env": "X_TOKEN"}))
    assert err.details == {"token_env": "X_TOKEN"}


def test_a_write_that_got_a_3xx_is_provider_changed_with_outcome_unknown() -> None:
    err = mapped(GatewayError("provider_changed", "redirect", outcome_unknown=True))
    assert err.code == "provider_changed" and err.outcome_unknown is True
    assert err.body()["outcome_unknown"] is True
    with pytest.raises(ValueError):
        TicketctlError("rejected", "m", outcome_unknown=True)


def test_state_error_is_exit_one_internal_and_names_the_state_reason() -> None:
    err = mapped(StateError("state_owner", "state directory is not owned by the current user"))
    assert (err.exit, err.code, err.reason) == (1, "internal", None)
    assert "state_owner" in err.message


def test_other_exceptions_are_not_mapped() -> None:
    assert from_exception(RuntimeError("x")) is None
    assert from_exception(NotImplementedError("x")) is None


def test_the_envelope_message_is_scrubbed_and_sanitized() -> None:
    sanitize.configure_scrubber([SECRET])
    body = TicketctlError("not_found", f"missing {SECRET}\x1b[0m\nline").body()
    assert SECRET not in json.dumps(body) and "<redacted>" in body["message"]
    assert "\x1b" not in body["message"] and "\n" not in body["message"]


def test_clean_message_scrubs_before_the_cut() -> None:
    sanitize.configure_scrubber([SECRET])
    text = "x" * 4085 + SECRET  # the secret straddles the cap
    assert SECRET[:10] not in clean_message(text)


def test_clean_message_keeps_the_marker_format() -> None:
    assert clean_message("a\nb\x1b[0m‮ ") == "a<U+000A>b<U+001B>[0m<U+202E><U+2028>"


# --- flags use the config duration grammar and the sanitizer grammars ------------------------


def test_timeout_flag_uses_the_config_duration_grammar() -> None:
    import argparse

    from ticketctl import config

    for text in ("1s", "90", "2m", "24h", "1000ms", "1d"):
        assert (
            flags.parse_duration(text)
            == config.parse_duration(
                text, minimum=flags.TIMEOUT_MIN, maximum=flags.TIMEOUT_MAX
            ).total_seconds()
        )
    for text in ("0", "0s", "25h", "-5", "abc", "1.5s", "01s", "", "1 s", "999ms", "2d"):
        with pytest.raises(argparse.ArgumentTypeError):
            flags.parse_duration(text)


def test_flag_grammars_are_the_sanitizer_grammars() -> None:
    assert flags.ACTOR_RE is sanitize.GRAMMARS["actor"].pattern
    assert flags.DECISION_ID_RE is sanitize.GRAMMARS["decision_id"].pattern
    assert flags.NUMERIC_ID_RE is sanitize.GRAMMARS["numeric_id"].pattern
    assert flags.SHA256_RE is sanitize.GRAMMARS["sha256"].pattern
    assert flags.dedupe_key_arg("a:b/c") == "a:b/c"
