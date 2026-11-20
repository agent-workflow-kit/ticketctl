"""Per-command wiring: config, state directory, credentials, scrubber, request log, gateway.

The first module that joins the others. A command builds a :class:`Runtime` (config loaded,
warnings shown), picks a site with ``selection.select_site`` and, only if it calls the provider,
opens a gateway with :func:`open_gateway`. That function is the one place a credential variable is
read: it resolves the selected site's token and email (``auth_required`` when unset or empty),
registers the token and the Basic value with the output scrub before anything can be printed, and
wires the gateway's request-log sink to ``audit.append_log``. ``sites`` and an offline ``validate``
never call it, so they never read a token variable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import audit, output
from .config import Config, LoadedConfig, load_config
from .errors import from_exception
from .flags import Globals
from .gateway import Deadline, Gateway, Target
from .sanitize import basic_auth_value, configure_scrubber
from .state import state_dir_path


@dataclass
class Runtime:
    g: Globals
    loaded: LoadedConfig
    env: Mapping[str, str]
    state_dir: Path

    @property
    def config(self) -> Config:
        return self.loaded.config

    def warn(self, text: str) -> None:
        """A non-error stderr line; ``--quiet`` drops it."""
        if not self.g.quiet:
            output.write(sys.stderr, text)

    def config_sha256(self) -> str:
        """Digest of the merged, validated configuration (the request log's ``config_sha256``)."""
        document = self.config.model_dump(mode="json", by_alias=True)
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def timeout_s(self) -> float:
        if self.g.timeout_s is not None:
            return self.g.timeout_s
        return self.config.timeout.total_seconds()


def load_runtime(
    g: Globals, *, env: Mapping[str, str] | None = None, show_warnings: bool = True
) -> Runtime:
    """Load the configuration (``--config`` or the default location) and show its warnings."""
    environment = os.environ if env is None else env
    loaded = load_config(Path(g.config) if g.config is not None else None, env=environment)
    rt = Runtime(g=g, loaded=loaded, env=environment, state_dir=state_dir_path(environment))
    for warning in loaded.warnings if show_warnings else ():
        rt.warn(f"warning: {warning}")
    return rt


def open_gateway(rt: Runtime, site_name: str) -> Gateway:
    """Resolve the site's credentials, arm the output scrub and return its gateway.

    Raises ``auth_required`` (naming the variable) for an unset or empty ``token_env`` or
    ``email_env``. Nothing is printed before the scrub knows the token.
    """
    site = rt.config.sites[site_name]
    target = Target.from_site(
        site_name, site, rt.env, rt.config.limits.retry_after_max.total_seconds()
    )
    configure_scrubber([target.token], basic_values=[basic_auth_value(target.email, target.token)])
    state_dir = rt.state_dir

    def log_sink(record: dict[str, object]) -> None:
        # The gateway's line carries `v` and `ts`; append_log adds both itself and refuses a
        # record that already has them.
        line = {k: v for k, v in record.items() if k not in ("v", "ts")}
        audit.append_log(state_dir, line, warn=rt.warn)

    def verbose(line: str) -> None:
        output.write(sys.stderr, line)

    return Gateway(
        target,
        deadline=Deadline(rt.timeout_s()),
        env=rt.env,
        log_sink=log_sink,
        verbose=verbose if rt.g.verbose else None,
        config_sha256=rt.config_sha256(),
    )


@contextmanager
def for_site(site: str) -> Iterator[None]:
    """Turn the modules' own errors into envelope errors that name the selected site."""
    try:
        yield
    except Exception as exc:
        err = from_exception(exc)
        if err is None:
            raise
        if err.site is None:
            err.site = site
        if err is exc:
            raise
        raise err from None


__all__ = ["Runtime", "for_site", "load_runtime", "open_gateway"]
