"""Handlers of ``attachment-add KEY --file PATH`` and ``attachment-download KEY ID --to PATH``
(docs/interface.md, ``attachment-add`` and ``attachment-download``).

* **Upload** is one step through the write flow engine (``writeflow.py``), like ``comment``: the
  file is read once in the prepare step (a regular file the user named, never a symbolic link or a
  path under ``/proc``, ``/sys`` or ``/dev``, at most ``MAX_ATTACHMENT_BYTES``, not empty, no
  configured secret in it) and the bytes that are hashed, shown and confirmed are the bytes that
  are sent. The file name sent is the base name of the path, and only a plain one (no control
  character, quote, backslash or slash, at most 255 characters and bytes, nothing the sanitizer
  would change). The media type is always ``application/octet-stream``: ticketctl guesses nothing.
* **Idempotency by content.** The provider keeps a second copy under the same name, so a re-run
  after a lost answer would duplicate the file. ``reduce`` reads the ticket's attachments first
  and, for those with the same name and the same size, downloads at most ``MAX_CANDIDATES`` of
  them and compares the sha256: an identical one is ``unchanged`` and its id is the result's
  ``attachment_id``. A candidate that cannot be read stops the command (absence is not proven),
  never sends. No dedupe key is needed or accepted.
* **Download** is a read: the attachment must be listed on the ticket named on the command line
  (its id alone is never enough), the content comes from
  ``GET /attachment/content/{id}?redirect=false`` (the one request that answers the bytes
  itself; the provider's redirect, an absolute URL, is never followed, and the ``content`` URL of
  the metadata is never used), at most
  ``MAX_ATTACHMENT_BYTES`` and exactly the size the metadata states. The bytes go only to the path
  after ``--to``, created with the rules of ``export --to`` (new file, mode 0600, an existing path
  is exit 20 ``already_exists``, no overwrite flag exists). The provider's file name is never used
  to build a path; it is printed sanitized.

Unverified against a real site (docs/live-verification.md, W23): that the upload answers a list
with the new attachment, that ``redirect=false`` answers the bytes, how a non-ASCII file name is
stored, the provider's size limit, and that a same-named upload is kept as a second attachment.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import Any

from . import commentcmd, selection, writeflow
from .errors import TicketctlError
from .exportcmd import create_new_file, remove_if_ours
from .flags import Globals
from .gateway import MAX_UPLOAD_NAME, UPLOAD_BAD_NAME, Upload
from .plan import Planned, ShownText, Step
from .readutil import object_error
from .runtime import for_site, load_runtime, open_gateway
from .sanitize import CAPS, GRAMMARS, open_input_fd, query, sanitize_field, segment
from .state import write_all
from .writeflow import (
    Context,
    PrepareContext,
    Seams,
    StepResult,
    WriteGateway,
    raise_for_write,
)

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_CANDIDATES = 5  # same-named, same-sized attachments compared by content
MAX_ATTACHMENTS = 500  # entries of the ticket's list that are looked at
ADD_TEMPLATE = "/rest/api/3/issue/{key}/attachments"
CONTENT_TEMPLATE = "/rest/api/3/attachment/content/{id}"
NAME_CAP = CAPS["name"]


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def _file_error(why: str) -> TicketctlError:
    return TicketctlError(
        "invalid_request", f"cannot use the attachment file: {why}", reason="invalid_file"
    )


# --- the file to upload -----------------------------------------------------------------------


def check_name(name: str) -> str:
    """``name`` when it is a plain file name ticketctl will send, else exit 22 ``invalid_file``."""
    if name in {"", ".", ".."}:
        raise _file_error("the path has no file name")
    if len(name) > MAX_UPLOAD_NAME or len(name.encode("utf-8", "surrogatepass")) > MAX_UPLOAD_NAME:
        raise _file_error("the file name is longer than 255 characters")
    if UPLOAD_BAD_NAME.search(name) or name != name.strip():
        raise _file_error("the file name has a control character, quote, backslash or space edge")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise _file_error("the file name is not valid text") from None
    if sanitize_field(name, cap=NAME_CAP)[0] != name:
        raise _file_error("the file name has characters that ticketctl does not send")
    return name


def read_upload(path: str) -> tuple[str, bytes]:
    """The name to send and the bytes of ``path``, under the Input files rules and the size cap."""
    fd = open_input_fd(path)  # InputError `invalid_file` for a link, a device, a FIFO, /proc ...
    try:
        if os.fstat(fd).st_size > MAX_ATTACHMENT_BYTES:
            raise _file_error("the file is larger than 10 MiB")
        chunks: list[bytes] = []
        remaining = MAX_ATTACHMENT_BYTES + 1
        while remaining > 0:
            try:
                chunk = os.read(fd, min(remaining, 1 << 20))
            except OSError:
                raise _file_error("the file cannot be read") from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise _file_error("the file is larger than 10 MiB")
    if not data:
        raise _file_error("the file is empty")
    return check_name(os.path.basename(path)), data


# --- the ticket's attachments ------------------------------------------------------------------


class Listing:
    """What was read of ``fields.attachment``.

    ``items`` are the readable entries of the first ``MAX_ATTACHMENTS``; ``truncated`` is true when
    the list was longer (the rest was not looked at); ``unreadable`` holds the names of entries with
    a valid id and a text name but no usable size.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.truncated = False
        self.unreadable: set[str] = set()


def scan_attachments(fields: dict[str, Any]) -> Listing:
    """Read ``fields.attachment`` into a :class:`Listing` (a non-list is ``provider_changed``).

    An entry that is not an object with a valid id, a text name and a non-negative integer size is
    not an item (it cannot be the file asked for); one with a valid id and a text name only lacks a
    size, and its name goes to ``unreadable``.
    """
    raw = fields.get("attachment")
    if not isinstance(raw, list):
        raise TicketctlError("provider_changed", "the provider answered no attachment list")
    listing = Listing()
    listing.truncated = len(raw) > MAX_ATTACHMENTS
    out = listing.items
    for item in raw[:MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        aid, name, size, mime = (item.get(k) for k in ("id", "filename", "size", "mimeType"))
        sized = isinstance(size, int) and not isinstance(size, bool) and size >= 0
        if GRAMMARS["numeric_id"].matches(aid) and isinstance(name, str) and not sized:
            listing.unreadable.add(name)
            continue
        if GRAMMARS["numeric_id"].matches(aid) and isinstance(name, str) and sized:
            out.append(
                {
                    "id": aid,
                    "filename": name,
                    "size": size,
                    "mime": mime if isinstance(mime, str) else "",
                }
            )
    return listing


def parse_attachments(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """The readable entries of ``fields.attachment``: ``{id, filename, size, mime}`` (raw text)."""
    return scan_attachments(fields).items


def fetch_content(gw: Any, attachment_id: str) -> bytes:
    """The bytes of one attachment (``redirect=false``; nothing the response names is followed)."""
    res = gw.request(
        "GET",
        f"/rest/api/3/attachment/content/{segment('numeric_id', attachment_id)}",
        template=CONTENT_TEMPLATE,
        query=query({"redirect": "false"}),
        binary=True,
    )
    if not 200 <= res.status < 300:
        raise object_error(res.status, "attachment")
    if len(res.raw) > MAX_ATTACHMENT_BYTES:
        raise TicketctlError(
            "invalid_request", "the attachment is larger than 10 MiB", reason="body_too_large"
        )
    return res.raw


# --- attachment-add ----------------------------------------------------------------------------


class AttachmentAddAction:
    """``attachment-add KEY --file PATH`` as a :class:`~ticketctl.writeflow.WriteAction`."""

    name = "attachment_add"
    dedupe_key: str | None = None
    yes_needs_dedupe_key = False

    def __init__(self, key: str, path: str) -> None:
        self.key = key
        self.path = path
        self.file_name = ""
        self.data = b""
        self.digest = ""

    def prepare(self, pc: PrepareContext) -> None:
        # the file name goes out as the multipart file name, which the body check does not see
        pc.check_secrets(os.path.basename(self.path))
        self.file_name, self.data = read_upload(self.path)
        # a secret is judged on the bytes read as Latin-1 (every byte is one character)
        pc.check_secrets(self.data.decode("latin-1"))
        self.digest = hashlib.sha256(self.data).hexdigest()

    def check(self, ctx: Context, planned: Planned) -> None:
        return None

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        fields = commentcmd.read_ticket(ctx, self.key, extra_fields="attachment")
        listing = scan_attachments(fields)
        if listing.truncated:
            raise TicketctlError(
                "lookup_incomplete",
                f"the ticket has more than {MAX_ATTACHMENTS} attachments, "
                "so it cannot be proven that none is this file",
            )
        if self.file_name in listing.unreadable:
            raise TicketctlError(
                "lookup_incomplete",
                "an attachment with this name has no readable size, "
                "so it cannot be proven that none is this file",
            )
        existing = self._identical(ctx, listing.items)
        size = len(self.data)
        shown = f"{self.file_name} ({size} bytes, sha256 {self.digest})"
        raw_status = fields.get("status")
        status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
        raw_cat = status.get("statusCategory")
        cat = raw_cat.get("key") if isinstance(raw_cat, dict) else None
        category = cat if cat in commentcmd.CATEGORIES else None
        steps = [] if existing is not None else [self._step()]
        return Planned(
            steps=steps,
            intent={"file_name": self.file_name, "file_sha256": self.digest, "file_bytes": size},
            facts={},
            changes=[{"name": "attachment", "from": None, "to": shown}],
            key=self.key,
            bodies=[{"sha256": self.digest, "bytes": size}],
            shown=[ShownText("attachment", shown, "line", self.digest)],
            hook=commentcmd.hook_facts(ctx, fields, status, category),
            attachment_id=existing,
        )

    def _identical(self, ctx: Context, items: list[dict[str, Any]]) -> str | None:
        """The id of an attachment with this name and these bytes, else None."""
        size = len(self.data)
        same = [a for a in items if a["filename"] == self.file_name and a["size"] == size]
        for item in same[:MAX_CANDIDATES]:
            if hashlib.sha256(fetch_content(ctx.gw, item["id"])).hexdigest() == self.digest:
                return str(item["id"])
        if len(same) > MAX_CANDIDATES:
            raise TicketctlError(
                "lookup_incomplete",
                "too many attachments with this name and size to prove that none is this file",
            )
        return None

    def _step(self) -> Step:
        key, name, data = self.key, self.file_name, self.data

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "POST",
                f"/rest/api/3/issue/{segment('ticket_key', key)}/attachments",
                template=ADD_TEMPLATE,
                upload=Upload(name, data),
            )
            raise_for_write(res, object_path=True)
            created = res.data[0] if isinstance(res.data, list) and len(res.data) == 1 else None
            aid = created.get("id") if isinstance(created, dict) else None
            ids = [aid] if isinstance(aid, str) and GRAMMARS["numeric_id"].matches(aid) else []
            return StepResult(ids, attachment_id=ids[0] if ids else None)

        return Step(
            "add_attachment", send, {"file_sha256": self.digest, "file_bytes": len(self.data)}
        )


def attachment_add(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    action = AttachmentAddAction(chosen.key, a.file)
    return writeflow.execute(rt, chosen.site, action, make_seams())


# --- attachment-download -----------------------------------------------------------------------


def attachment_download(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    site, key, wanted = chosen.site, chosen.key, a.attachment_id
    with for_site(site):
        gw = open_gateway(rt, site)
        res = gw.request(
            "GET",
            f"/rest/api/3/issue/{segment('ticket_key', key)}",
            template=commentcmd.ISSUE_TEMPLATE,
            query=query({"fields": "attachment"}),
        )
        if not 200 <= res.status < 300:
            raise object_error(res.status, "ticket")
        fields = res.data.get("fields") if isinstance(res.data, dict) else None
        if not isinstance(fields, dict):
            raise TicketctlError("provider_changed", "the provider answered no ticket fields")
        listing = scan_attachments(fields)
        item = next((x for x in listing.items if x["id"] == wanted), None)
        if item is None and listing.truncated:
            raise TicketctlError(
                "lookup_incomplete",
                f"the ticket has more than {MAX_ATTACHMENTS} attachments and the id is not among "
                "the first ones, so it cannot be told whether it is on the ticket",
            )
        if item is None:
            raise TicketctlError(
                "not_found", "the attachment is not on that ticket, or you have no permission"
            )
        if item["size"] > MAX_ATTACHMENT_BYTES:
            raise TicketctlError(
                "invalid_request", "the attachment is larger than 10 MiB", reason="body_too_large"
            )
        _refuse_existing(a.to)
        data = fetch_content(gw, wanted)
    if len(data) != item["size"]:
        raise TicketctlError(
            "provider_changed", "the attachment's size differs from its metadata; nothing was saved"
        )
    _save(a.to, data)
    return {
        "key": key,
        "site": site,
        "id": wanted,
        "name": sanitize_field(item["filename"], cap=CAPS["attachment_name"])[0],
        "mime": sanitize_field(item["mime"], cap=NAME_CAP)[0],
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "path": a.to,
    }


def _refuse_existing(path: str) -> None:
    """A cheap early answer before the download; the real guard is ``O_EXCL`` in ``_save``."""
    if path and "\0" not in path and os.path.lexists(path):
        raise TicketctlError("already_exists", "--to names a path that already exists")


def _save(path: str, data: bytes) -> None:
    fd, pfd, name = create_new_file(path)
    try:
        write_all(fd, data)
    except BaseException:
        remove_if_ours(pfd, name, fd)
        raise
    finally:
        os.close(fd)
        os.close(pfd)


def human_download(r: dict[str, Any]) -> str:
    return (
        f"{r['key']}: attachment {r['id']} saved to {r['path']} "
        f"({r['size']} bytes, sha256 {r['sha256']})"
    )
