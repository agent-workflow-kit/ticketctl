"""Config loading and validation.

`load_config` reads ``config.yaml`` then ``config.d/*.yaml`` (or one ``--config`` file), merges
them, validates the result with Pydantic models and runs the loader checks that JSON Schema cannot
express. Every failure is a `ConfigError` (exit 2, code `invalid_config`) that names the key path
and the constraint, never the offending value or a source line.

Everything read here is hostile input: the files, the drop-ins and the environment scalars.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import pwd
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    StringConstraints,
    ValidationError,
    model_validator,
)

MAX_FILE_BYTES = 1024 * 1024
MAX_DROPINS = 64
MAX_YAML_DEPTH = 32
MAX_BOARD_ID = 10**18 - 1

ENV_PREFIX = "TICKETCTL_"
ENV_SCALARS = ("TIMEOUT", "DEFAULT_SITE")
# Variables of the interface that are not config keys (hook variables, debug and live switches).
ENV_NON_CONFIG = frozenset({"HOOK", "ID", "OBJECT_TYPE", "DEBUG", "LIVE"})

LOGICAL_FIELDS = (
    "estimate",
    "acceptance_criteria",
    "sprint",
    "parent",
    "epic",
    "labels",
    "priority",
    "assignee",
    "resolution",
    "due_date",
)
HOOK_NAMES = tuple(
    f"{when}-{kind}"
    for kind in ("transition", "comment", "create", "update", "sprint")
    for when in ("pre", "post")
)

SITE_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
SNAKE_NAME = r"[a-z][a-z0-9_]{0,63}"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ConfigError(Exception):
    """An invalid configuration: exit 2, code ``invalid_config``.

    TODO(#5): map to the error envelope (code, reason, message) in cli.py.
    """

    code = "invalid_config"
    exit_code = 2

    def __init__(
        self,
        constraint: str,
        *,
        key: str | None = None,
        source: Path | str | None = None,
        reason: str = "config_invalid",
    ) -> None:
        self.constraint = constraint
        self.key = key
        self.source = None if source is None else _display(source)
        self.reason = reason
        prefix = key if key is not None else self.source
        super().__init__(f"{prefix}: {constraint}" if prefix else constraint)


def _display(value: object, limit: int = 200) -> str:
    """Printable form of an untrusted string: controls and non-ASCII become `?`."""
    text = str(value)[:limit]
    return re.sub(r"[^\x21-\x7e]", "?", text) if re.search(r"[^\x20-\x7e]", text) else text


def _seg(value: object) -> str:
    """A key-path segment: short and made of safe characters only."""
    return re.sub(r"[^A-Za-z0-9_.-]", "?", str(value))[:64]


# --- durations and shared field types ---------------------------------------------------------

_DURATION = re.compile(r"(0|[1-9][0-9]{0,8})(ms|s|m|h|d)?")
_UNITS = {
    None: timedelta(seconds=1),
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}
_DURATION_HELP = "an integer with a unit (ms, s, m, h, d) or a bare integer of seconds"


def parse_duration(
    value: object,
    *,
    zero_ok: bool = False,
    minimum: timedelta | None = None,
    maximum: timedelta | None = None,
    whole_seconds: bool = False,
) -> timedelta:
    """The one duration grammar: `<integer>(ms|s|m|h|d)` or a bare integer meaning seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"must be {_DURATION_HELP}")
    match = _DURATION.fullmatch(str(value)) if len(str(value)) <= 12 else None
    if match is None:
        raise ValueError(f"must be {_DURATION_HELP}")
    result = int(match.group(1)) * _UNITS[match.group(2)]
    if result == timedelta(0):
        if zero_ok:
            return result
        raise ValueError("must be greater than zero")
    if whole_seconds and result % timedelta(seconds=1):
        raise ValueError("must be a whole number of seconds")
    if minimum is not None and result < minimum:
        raise ValueError(f"must be at least {_fmt(minimum)}")
    if maximum is not None and result > maximum:
        raise ValueError(f"must be at most {_fmt(maximum)}")
    return result


def _fmt(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


_ONE_DAY = timedelta(hours=24)
_ONE_SECOND = timedelta(seconds=1)
_ONE_YEAR = timedelta(days=365)


def _converter(**options: Any) -> Callable[[object], timedelta]:
    def convert(value: object) -> timedelta:
        return parse_duration(value, **options)

    return convert


Timeout = Annotated[timedelta, BeforeValidator(_converter(minimum=_ONE_SECOND, maximum=_ONE_DAY))]
PositiveDuration = Annotated[timedelta, BeforeValidator(_converter(maximum=_ONE_DAY))]
CacheDuration = Annotated[timedelta, BeforeValidator(_converter(zero_ok=True, maximum=_ONE_YEAR))]
HookTimeout = Annotated[
    timedelta,
    BeforeValidator(
        _converter(minimum=_ONE_SECOND, maximum=timedelta(seconds=600), whole_seconds=True)
    ),
]


def _matches(pattern: str) -> AfterValidator:
    rx = re.compile(pattern)

    def check(value: str) -> str:
        if rx.fullmatch(value) is None:
            raise ValueError(f"must match {pattern}")
        return value

    return AfterValidator(check)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _no_control(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("must not contain control characters")
    return value


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


def _unique(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        raise ValueError("must not contain duplicates")
    return value


Label256 = Annotated[
    str, StringConstraints(min_length=1, max_length=256), AfterValidator(_no_control)
]


def _normalize_actor_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", value)[:64]


SiteName = Annotated[str, _matches(SITE_NAME)]
AccountId = Annotated[str, _matches(r"[A-Za-z0-9:_.-]{1,128}")]
EnvName = Annotated[str, _matches(r"[A-Za-z_][A-Za-z0-9_]{0,127}")]
Email = Annotated[
    str,
    StringConstraints(min_length=1, max_length=320),
    _matches(r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+"),
]
ProjectKey = Annotated[str, _matches(r"[A-Z][A-Z0-9_]{0,63}")]
FieldId = Annotated[str, _matches(r"[A-Za-z0-9_.:-]{1,128}")]
UniqueTexts = Annotated[list[Label256], AfterValidator(_unique)]
UniqueProjects = Annotated[list[ProjectKey], AfterValidator(_unique)]


# --- url --------------------------------------------------------------------------------------

_HOST_LABELS = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def _split_url(raw: str) -> tuple[str, str, int | None, str]:
    """Split and normalize a site URL into (scheme, host, port, path); `ValueError` if refused."""
    if len(raw) > 2048:
        raise ValueError("must be at most 2048 characters")
    if re.search(r"[^\x21-\x7e]", raw):
        raise ValueError("must be printable ASCII without spaces")
    if "\\" in raw or "%" in raw:
        raise ValueError("must not contain backslashes or percent escapes")
    if "?" in raw or "#" in raw:
        raise ValueError("must not have a query or fragment")
    scheme, sep, rest = raw.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in ("https", "http"):
        raise ValueError("must be an http or https URL")
    netloc, slash, tail = rest.partition("/")
    path = slash + tail
    if "@" in netloc:
        raise ValueError("must not contain userinfo")
    host, port = _split_netloc(netloc)
    if path not in ("", "/"):
        body = path[:-1] if path.endswith("/") else path
        for part in body.split("/")[1:]:
            if part in ("", ".", ".."):
                raise ValueError("must not have empty, '.' or '..' path segments")
    path = path.rstrip("/")
    if port == (443 if scheme == "https" else 80):
        port = None
    return scheme, host, port, path


def _split_netloc(netloc: str) -> tuple[str, int | None]:
    if netloc.startswith("["):
        literal, bracket, after = netloc[1:].partition("]")
        if not bracket or (after and not after.startswith(":")):
            raise ValueError("must have a valid host")
        try:
            host = str(ipaddress.IPv6Address(literal))
        except ValueError:
            raise ValueError("must have a valid host") from None
        port_text = after[1:] if after else None
        host_text = host
    else:
        if netloc.count(":") > 1:
            raise ValueError("must have a valid host")
        name, colon, port_part = netloc.partition(":")
        host_text = name.lower()
        port_text = port_part if colon else None
        if len(host_text) > 253 or _HOST_LABELS.fullmatch(host_text) is None:
            raise ValueError("must have a valid host")
    port: int | None = None
    if port_text is not None:
        if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 1 <= int(port_text) <= 65535:
            raise ValueError("must have a valid port")
        port = int(port_text)
    return host_text, port


def _join_url(scheme: str, host: str, port: int | None, path: str) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}{f':{port}' if port is not None else ''}{path}"


def _normalize_url(value: str) -> str:
    return _join_url(*_split_url(value))


Url = Annotated[str, AfterValidator(_normalize_url)]


# --- models -----------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FieldMapping(_Model):
    """A provider field selected by exactly one of name, id or schema."""

    name: Label256 | None = None
    id: FieldId | None = None
    schema_: Label256 | None = Field(None, alias="schema")

    @model_validator(mode="after")
    def _exactly_one(self) -> FieldMapping:
        if sum(v is not None for v in (self.name, self.id, self.schema_)) != 1:
            raise ValueError("exactly one of name, id and schema is required")
        return self


class Fields(_Model):
    estimate: FieldMapping | None = None
    acceptance_criteria: FieldMapping | None = None
    sprint: FieldMapping | None = None
    parent: FieldMapping | None = None
    epic: FieldMapping | None = None
    labels: FieldMapping | None = None
    priority: FieldMapping | None = None
    assignee: FieldMapping | None = None
    resolution: FieldMapping | None = None
    due_date: FieldMapping | None = None
    extra: dict[str, FieldMapping] = {}


class Transitions(_Model):
    default_resolution: Label256 | None = None
    assign_on_start: Literal["me"] | None = None


class Timeouts(_Model):
    connect: PositiveDuration = timedelta(seconds=10)
    read: PositiveDuration = timedelta(seconds=30)


def _board(value: object) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("must be a board id (positive integer) or a board name")
    if isinstance(value, int):
        if not 1 <= value <= MAX_BOARD_ID:
            raise ValueError("must be a positive integer of at most 18 digits")
        return value
    if not 1 <= len(value) <= 256:
        raise ValueError("must be 1 to 256 characters")
    if not value.strip():
        raise ValueError("must not be blank")
    return _no_control(value)


class Site(_Model):
    provider: Literal["jira"] = "jira"
    url: Url
    allow_http: bool = False
    account_id: AccountId | None = None
    blocking_link_types: UniqueTexts = ["Blocks"]
    email: Email | None = None
    email_env: EnvName | None = None
    token_env: EnvName
    projects: UniqueProjects = []
    board: Annotated[int | str, PlainValidator(_board)] | None = None
    fields: Fields = Fields()
    queries: dict[
        str,
        Annotated[str, StringConstraints(min_length=1, max_length=4096), AfterValidator(_no_nul)],
    ] = {}
    transitions: Transitions = Transitions()
    cache_ttl: CacheDuration = timedelta(hours=24)
    timeouts: Timeouts = Timeouts()


class Me(_Model):
    name: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=128),
            AfterValidator(_normalize_actor_name),
        ]
        | None
    ) = None
    jira: AccountId | None = None


class Attribution(_Model):
    enabled: bool = True
    template: Annotated[
        str,
        StringConstraints(min_length=1, max_length=300),
        AfterValidator(lambda v: _no_control_ascii(v)),
    ] = "Drafted with AI assistance and posted via ticketctl."


def _no_control_ascii(value: str) -> str:
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("must not contain control characters")
    return value


class Limits(_Model):
    body_chars: Annotated[int, Field(ge=1, le=32767)] = 30000
    retry_after_max: CacheDuration = timedelta(seconds=60)


class Hook(_Model):
    cmd: Annotated[str, StringConstraints(min_length=1, max_length=4096), AfterValidator(_no_nul)]
    timeout: HookTimeout = timedelta(seconds=30)
    async_: bool = Field(False, alias="async")


class Config(_Model):
    sites: dict[str, Site] = {}
    default_site: SiteName | None = None
    timeout: Timeout = timedelta(seconds=120)
    me: Me = Me()
    attribution: Attribution = Attribution()
    limits: Limits = Limits()
    hooks: dict[str, list[Hook]] = {}

    def require_sites(self) -> None:
        """Raise `no_sites` when nothing is configured (zero sites is an `invalid_config`)."""
        if not self.sites:
            raise ConfigError("no sites are configured", key="sites", reason="no_sites")


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    warnings: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)


# --- YAML -------------------------------------------------------------------------------------


class _Rejected(Exception):
    def __init__(self, constraint: str) -> None:
        super().__init__(constraint)
        self.constraint = constraint


_STR_TAG = "tag:yaml.org,2002:str"
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader without anchors, aliases, tags, merge keys, duplicate or non-string keys."""

    _depth = 0

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            raise _Rejected("YAML aliases are not allowed")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise _Rejected("YAML anchors are not allowed")
        if getattr(event, "tag", None) is not None:
            raise _Rejected("explicit YAML tags are not allowed")
        self._depth += 1
        try:
            if self._depth > MAX_YAML_DEPTH:
                raise _Rejected(f"YAML nesting is deeper than {MAX_YAML_DEPTH} levels")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _value in node.value:
            if key_node.tag == _MERGE_TAG:
                raise _Rejected("YAML merge keys are not allowed")
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != _STR_TAG:
                raise _Rejected("mapping keys must be strings")
            if key_node.value in seen:
                raise _Rejected("duplicate mapping key")
            seen.add(key_node.value)
        return super().construct_mapping(node, deep)


# YAML 1.1 reads `010`, `0x10`, `0b11`, `1_0`, `1:30` and `+5` as integers; a duration or board must
# never come from those. Only plain decimal integers (optionally negative, so a negative is a range
# error) resolve as int on this loader; everything else stays a string and fails the grammar.
_INT_TAG = "tag:yaml.org,2002:int"
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != _INT_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(
    _INT_TAG, re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789")
)


def _parse_yaml(text: str, source: Path) -> dict[str, Any]:
    # The message is captured in the handler and raised after it, so the ConfigError has neither a
    # cause nor a context: the parser exceptions can echo the input.
    failure: str | None = None
    data: Any = None
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except _Rejected as exc:
        failure = exc.constraint
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        failure = f"invalid YAML (line {mark.line + 1})" if mark is not None else "invalid YAML"
    except ValueError:
        failure = "invalid YAML scalar"
    except RecursionError:
        failure = "YAML nesting is too deep"
    if failure is not None:
        raise ConfigError(failure, source=source)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("the top level must be a mapping", source=source)
    return data


# --- reading files ----------------------------------------------------------------------------


def _read_file(path: Path) -> str:
    """Read one config file: a regular file, owned by the user or root, not world-writable."""
    failure: str | None = None
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError as exc:
        failure = f"cannot open the file ({errno.errorcode.get(exc.errno or 0, 'error')})"
    if failure is not None:
        raise ConfigError(failure, source=path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("must be a regular file", source=path)
        if info.st_uid not in (os.geteuid(), 0):
            raise ConfigError("owner must be the current user or root", source=path)
        if info.st_mode & stat.S_IWOTH:
            raise ConfigError("is world-writable", source=path)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            try:
                chunk = os.read(fd, MAX_FILE_BYTES + 1 - total)
            except OSError as exc:
                failure = f"cannot read the file ({errno.errorcode.get(exc.errno or 0, 'error')})"
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if failure is not None:
            raise ConfigError(failure, source=path)
        if total > MAX_FILE_BYTES:
            raise ConfigError(f"is too large (maximum {MAX_FILE_BYTES} bytes)", source=path)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        failure = "is not valid UTF-8"
    raise ConfigError(failure, source=path)


def _exists(path: Path) -> bool:
    failure: str | None = None
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        failure = f"cannot access the path ({errno.errorcode.get(exc.errno or 0, 'error')})"
    if failure is not None:
        raise ConfigError(failure, source=path)
    return True


def _dropins(directory: Path) -> list[Path]:
    failure: str | None = None
    info: os.stat_result | None = None
    try:
        info = os.stat(directory)
    except FileNotFoundError:
        return []
    except OSError as exc:
        failure = f"cannot access the directory ({errno.errorcode.get(exc.errno or 0, 'error')})"
    if failure is not None or info is None:
        raise ConfigError(failure or "cannot access the directory", source=directory)
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError("must be a directory", source=directory)
    if info.st_uid not in (os.geteuid(), 0):
        raise ConfigError("owner must be the current user or root", source=directory)
    if info.st_mode & stat.S_IWOTH:
        raise ConfigError("is world-writable", source=directory)
    names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".yaml") and not entry.name.startswith("."):
                    names.append(entry.name)
                    if len(names) > MAX_DROPINS:
                        break
    except OSError as exc:
        failure = f"cannot list the directory ({errno.errorcode.get(exc.errno or 0, 'error')})"
    if failure is not None:
        raise ConfigError(failure, source=directory)
    if len(names) > MAX_DROPINS:
        raise ConfigError(f"too many drop-in files (maximum {MAX_DROPINS})", source=directory)
    return [directory / n for n in sorted(names)]


def _config_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "ticketctl"
    home = env.get("HOME") or _os_home()
    return Path(home) / ".config" / "ticketctl"


def _os_home() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        return "/nonexistent"


def _os_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return "unknown"


# --- merge ------------------------------------------------------------------------------------


def _merge(
    base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Maps merge, `hooks.<name>` lists append, every other value (lists included) replaces."""
    merged = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        here = (*path, key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value, here)
        elif (
            len(here) == 2
            and here[0] == "hooks"
            and isinstance(current, list)
            and isinstance(value, list)
        ):
            merged[key] = [*current, *value]
        else:
            merged[key] = value
    return merged


# --- validation -------------------------------------------------------------------------------


def _path(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + _seg(part)
    return out


def _reject_nulls(node: object, loc: tuple[int | str, ...] = ()) -> None:
    """An explicit null is never a value: omit the key instead (no key accepts null)."""
    if isinstance(node, dict):
        for key, value in node.items():
            _reject_nulls(value, (*loc, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_nulls(value, (*loc, index))
    elif node is None and loc:
        raise ConfigError("must not be null (omit the key instead)", key=_path(loc))


def _validate(merged: dict[str, Any]) -> Config:
    _reject_nulls(merged)
    failure: tuple[str, str | None] | None = None
    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_context=False, include_input=False)[0]
        message = first["msg"].removeprefix("Value error, ")
        extra = exc.error_count() - 1
        more = f" ({extra} more error{'s' if extra != 1 else ''})" if extra else ""
        failure = (message + more, _path(first["loc"]) or None)
    raise ConfigError(failure[0], key=failure[1])


def _check(config: Config) -> None:
    """The loader checks JSON Schema cannot express; each names its key path."""
    for name, site in config.sites.items():
        base = f"sites.{_seg(name)}"
        if re.fullmatch(SITE_NAME, name) is None:
            raise ConfigError(f"name must match {SITE_NAME}", key=base)
        if (site.email is None) == (site.email_env is None):
            raise ConfigError("exactly one of email and email_env is required", key=f"{base}.email")
        scheme, host, _port, _path_ = _split_url(site.url)
        if scheme == "http" and not (site.allow_http and host in LOOPBACK_HOSTS):
            raise ConfigError(
                "an http url needs allow_http and a loopback host (127.0.0.1, ::1 or localhost)",
                key=f"{base}.url",
            )
        for qname in site.queries:
            _check_name(qname, f"{base}.queries")
        for xname in site.fields.extra:
            _check_name(xname, f"{base}.fields.extra")
    for hname, hooks in config.hooks.items():
        if hname not in HOOK_NAMES:
            raise ConfigError("unknown hook name", key=f"hooks.{_seg(hname)}")
        for i, hook in enumerate(hooks):
            if hook.async_ and hname.startswith("pre-"):
                raise ConfigError(
                    "async is allowed only for post- hooks", key=f"hooks.{hname}[{i}].async"
                )


def _check_name(name: str, parent: str) -> None:
    key = f"{parent}.{_seg(name)}"
    if re.fullmatch(SNAKE_NAME, name) is None:
        raise ConfigError(f"name must match {SNAKE_NAME}", key=key)
    if name in LOGICAL_FIELDS:
        raise ConfigError("name must not be a logical field name", key=key)


def _check_default_site(config: Config, site: str | None, key: str) -> None:
    if site is not None and site not in config.sites:
        raise ConfigError("must name a configured site", key=key)


def _apply_env(config: Config, env: Mapping[str, str]) -> tuple[Config, list[str]]:
    update: dict[str, Any] = {}
    # The file's own default_site is checked first, so a valid environment value cannot mask it.
    _check_default_site(config, config.default_site, "default_site")
    timeout = env.get(f"{ENV_PREFIX}TIMEOUT", "")
    if timeout:
        problem: str | None = None
        try:
            update["timeout"] = parse_duration(timeout, minimum=_ONE_SECOND, maximum=_ONE_DAY)
        except ValueError as exc:
            problem = str(exc)
        if problem is not None:
            raise ConfigError(problem, key=f"{ENV_PREFIX}TIMEOUT")
    default_site = env.get(f"{ENV_PREFIX}DEFAULT_SITE", "")
    if default_site:
        default_key = f"{ENV_PREFIX}DEFAULT_SITE"
        if re.fullmatch(SITE_NAME, default_site) is None:
            raise ConfigError(f"must match {SITE_NAME}", key=default_key)
        if config.sites:  # with no sites, `require_sites` reports no_sites instead
            _check_default_site(config, default_site, default_key)
        update["default_site"] = default_site
    top_level = {k for k in Config.model_fields if k not in ("timeout", "default_site")}
    warnings: list[str] = []
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX):
            continue
        suffix = name[len(ENV_PREFIX) :]
        if suffix in ENV_SCALARS or suffix in ENV_NON_CONFIG:
            continue
        if "__" in suffix or suffix.lower() in top_level:
            warnings.append(
                f"{_seg(name)}: ignored; only {ENV_PREFIX}TIMEOUT and "
                f"{ENV_PREFIX}DEFAULT_SITE are read from the environment"
            )
    return (config.model_copy(update=update) if update else config), warnings


def load_config(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> LoadedConfig:
    """Load, merge and validate the configuration.

    With `path` only that file is read (and it must exist). Otherwise `config.yaml` then
    `config.d/*.yaml` under `$XDG_CONFIG_HOME/ticketctl` (default `~/.config/ticketctl`) are read;
    a missing default config is an empty configuration. `env` defaults to `os.environ`.
    """
    environment: Mapping[str, str] = os.environ if env is None else env
    if path is not None:
        files = [path]
    else:
        directory = _config_dir(environment)
        main = directory / "config.yaml"
        files = ([main] if _exists(main) else []) + _dropins(directory / "config.d")
    merged: dict[str, Any] = {}
    for file in files:
        merged = _merge(merged, _parse_yaml(_read_file(file), file))
    config = _validate(merged)
    _check(config)
    if config.me.name is None:
        config = config.model_copy(
            update={"me": config.me.model_copy(update={"name": _normalize_actor_name(_os_user())})}
        )
    config, warnings = _apply_env(config, environment)
    return LoadedConfig(config=config, warnings=warnings, files=files)
