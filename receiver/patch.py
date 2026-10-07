"""Parsing and applying the patch a fix phase sends in place of whole files.

The patch is `git diff --binary` output taken against the grant's base
commit. The receiver has no working tree and no git binary: it opens pull
requests through GitHub's Git Data API, which builds blobs only from whole
content. So it reads each base file the patch modifies, applies the hunks
here, and uploads the result.

Application is strict, stricter than `git apply`. A hunk applies only at
the exact line its header names, with every context line matching byte for
byte: no fuzz, no offset, no three-way merge. And git writes the blob id of
each file before and after the change on the patch's `index` line, so the
result is checked against the second one. A patch therefore lands exactly as
the session produced it or not at all, and a base that differs anywhere the
hunks do not reach still fails the check.

Nothing here decides what a patch may touch; `receiver.autofix` holds that
policy, so both payload forms obey one copy of it. This module only turns
bytes into changes and refuses bytes it cannot read with certainty.
"""

from __future__ import annotations

import base64
import hashlib
import re
import zlib
from dataclasses import dataclass

DEV_NULL = "/dev/null"

# git abbreviates blob ids on text hunks' index lines, even with --binary,
# unless --full-index is also given. Seven characters is git's minimum.
_INDEX_RE = re.compile(rb"index ([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})(?: ([0-7]{6}))?")
_MODE_RE = re.compile(rb"[0-7]{6}")
_HUNK_RE = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_RE = re.compile(rb"(literal|delta) (\d+)")

# Headers git prints between `diff --git` and the body that carry nothing
# the receiver needs: rename detection's score, and -B's rewrite score.
_IGNORED_HEADERS = (b"similarity index ", b"dissimilarity index ")

_ESCAPES = {
    ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11,
    ord("f"): 12, ord("r"): 13, ord('"'): 34, ord("\\"): 92,
}


class PatchError(ValueError):
    """A patch the receiver cannot parse or cannot apply; the message says
    which file and why, and is the failure reason the session reads back."""


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # (tag, line) in patch order; tag is b" ", b"-", or b"+", and the line
    # keeps its newline unless git marked it as having none.
    lines: tuple[tuple[bytes, bytes], ...]


@dataclass(frozen=True)
class BinaryHunk:
    kind: str  # "literal" (the whole new content) or "delta" (against the old)
    data: bytes  # inflated


@dataclass(frozen=True)
class FileChange:
    """One file's section of the patch. A created file has no old path, a
    deleted one no new path, and a rename has two different paths."""

    old_path: str | None
    new_path: str | None
    old_mode: str | None  # None when the patch does not say, e.g. a pure rename
    new_mode: str | None
    old_blob: str | None  # hex prefix from the index line; None when absent
    new_blob: str | None  # None when the content does not change
    hunks: tuple[Hunk, ...] = ()
    binary: BinaryHunk | None = None

    @property
    def path(self) -> str:
        """The name to report this change under."""
        return self.new_path or self.old_path or ""

    @property
    def paths(self) -> tuple[str, ...]:
        """Every repository path this change touches, old side first."""
        return tuple(dict.fromkeys(p for p in (self.old_path, self.new_path) if p))

    @property
    def modes(self) -> tuple[tuple[str, str], ...]:
        """Each mode the patch declares, paired with the path it applies to."""
        declared = []
        if self.old_path and self.old_mode:
            declared.append((self.old_path, self.old_mode))
        if self.new_path and self.new_mode:
            declared.append((self.new_path, self.new_mode))
        return tuple(declared)

    @property
    def rewrites(self) -> bool:
        """Whether applying this change produces new content to upload. A
        deletion, a pure rename, and a mode change reuse the base blob."""
        return self.new_path is not None and self.new_blob is not None


def blob_sha(content: bytes) -> str:
    """The id git gives a blob holding `content`."""
    digest = hashlib.sha1(b"blob %d\0" % len(content))
    digest.update(content)  # not concatenated: a large file would be copied whole
    return digest.hexdigest()


def blob_matches(full: str, given: str) -> bool:
    """Whether `given`, a full or abbreviated id from an index line, names
    the blob whose full id is `full`."""
    return len(given) >= 7 and full.startswith(given)


def _split_lines(data: bytes) -> list[bytes]:
    """`data` split after each LF only. bytes.splitlines would also split on
    a bare CR, which is content in a CRLF file, not a line break."""
    parts = data.split(b"\n")
    out = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _unquote(raw: bytes) -> tuple[bytes, bytes]:
    """Read one C-quoted name from the start of `raw`, the form git uses
    for a path holding a space-adjacent quote, a control character, or (by
    default) any non-ASCII byte. Returns the name and what follows it."""
    out = bytearray()
    i = 1
    while i < len(raw):
        char = raw[i]
        if char == ord('"'):
            return bytes(out), raw[i + 1 :]
        if char == ord("\\"):
            nxt = raw[i + 1 : i + 2]
            if nxt and nxt[0] in _ESCAPES:
                out.append(_ESCAPES[nxt[0]])
                i += 2
                continue
            digits = raw[i + 1 : i + 4]
            if len(digits) == 3 and all(ord("0") <= d <= ord("7") for d in digits):
                out.append(int(digits, 8))
                i += 4
                continue
            raise PatchError(f"bad escape in quoted name {raw!r}")
        out.append(char)
        i += 1
    raise PatchError(f"unterminated quoted name {raw!r}")


def _name(raw: bytes, prefix: bytes | None, *, quoted: bool | None = None) -> str:
    """One path as git printed it, without its quoting and its a/ or b/
    prefix, decoded as UTF-8. `quoted=False` says the caller already
    unquoted it."""
    if quoted is not False and raw.startswith(b'"'):
        raw, rest = _unquote(raw)
        if rest:
            raise PatchError(f"unexpected text after a quoted name: {rest!r}")
    if prefix is not None:
        if not raw.startswith(prefix):
            raise PatchError(
                f"name {raw!r} lacks its {prefix.decode()} prefix; send plain git diff output"
            )
        raw = raw[len(prefix) :]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"name {raw!r} is not UTF-8") from exc


def _header_names(rest: bytes) -> tuple[str, str] | None:
    """The two names on a `diff --git` line, when they can be read without
    guessing. Unquoted names may contain spaces, so they are read only when
    both sides name the same path, the one case git's own reader trusts. A
    rename prints its names again on `rename from`/`rename to`, and a
    content change on `---`/`+++`, so None loses nothing."""
    if rest.startswith(b'"'):
        first, after = _unquote(rest)
        if not after.startswith(b" "):
            raise PatchError(f"malformed diff --git line: {rest!r}")
        return _name(first, b"a/", quoted=False), _name(after[1:], b"b/")
    if len(rest) % 2 == 0:
        return None
    half = len(rest) // 2
    first, sep, second = rest[:half], rest[half : half + 1], rest[half + 1 :]
    if sep != b" " or first[:2] != b"a/" or second[:2] != b"b/" or first[2:] != second[2:]:
        return None
    return _name(first, b"a/"), _name(second, b"b/")


def _side(raw: bytes, prefix: bytes) -> str | None:
    """The path on a `---` or `+++` line, None for /dev/null. git appends a
    tab to a name holding a space, so a patch tool can find where it ends."""
    if raw.endswith(b"\t"):
        raw = raw[:-1]
    if raw == DEV_NULL.encode():
        return None
    return _name(raw, prefix)


def _mode(value: bytes, line: bytes) -> str:
    if not _MODE_RE.fullmatch(value):
        raise PatchError(f"malformed mode in {line!r}")
    return value.decode()


def _parse_hunk(lines: list[bytes], i: int, path: str) -> tuple[Hunk, int]:
    match = _HUNK_RE.match(lines[i])
    if not match:
        raise PatchError(f"malformed hunk header for {path}: {lines[i]!r}")
    old_start, new_start = int(match[1]), int(match[3])
    old_count = int(match[2]) if match[2] is not None else 1
    new_count = int(match[4]) if match[4] is not None else 1
    body: list[list[bytes]] = []
    old_left, new_left = old_count, new_count
    i += 1
    while old_left > 0 or new_left > 0:
        if i >= len(lines):
            raise PatchError(f"patch ends inside a hunk for {path}")
        line = lines[i]
        tag = line[:1]
        if tag == b"\\":
            _mark_no_newline(body, path)
            i += 1
            continue
        if tag == b" ":
            old_left, new_left = old_left - 1, new_left - 1
        elif tag == b"-":
            old_left -= 1
        elif tag == b"+":
            new_left -= 1
        else:
            raise PatchError(f"malformed hunk line for {path}: {line!r}")
        if old_left < 0 or new_left < 0:
            raise PatchError(f"hunk for {path} has more lines than its header counts")
        body.append([tag, line[1:]])
        i += 1
    # The marker for a hunk's final line follows it, after the counts are met.
    if i < len(lines) and lines[i].startswith(b"\\"):
        _mark_no_newline(body, path)
        i += 1
    hunk = Hunk(
        old_start, old_count, new_start, new_count,
        tuple((tag, line) for tag, line in body),
    )
    return hunk, i


def _mark_no_newline(body: list[list[bytes]], path: str) -> None:
    """Apply a `\\ No newline at end of file` marker to the line before it."""
    if not body or not body[-1][1].endswith(b"\n"):
        raise PatchError(f"misplaced no-newline marker for {path}")
    body[-1][1] = body[-1][1][:-1]


def _binary_size(code: int) -> int:
    """Bytes on one line of a binary hunk, from the line's first character."""
    if ord("A") <= code <= ord("Z"):
        return code - ord("A") + 1
    if ord("a") <= code <= ord("z"):
        return code - ord("a") + 27
    raise PatchError("malformed binary hunk line")


class _Budget:
    """What the binary hunks of one patch may still inflate to, in total. A
    per-hunk cap alone would let twenty small hunks each claim all of it."""

    def __init__(self, total: int):
        self.total = self.left = total

    def spend(self, size: int, path: str) -> None:
        if size > self.left:
            raise PatchError(
                f"binary content for {path} takes the patch past the {self.total} bytes "
                "of binary content it may carry; it is larger than the receiver rewrites"
            )
        self.left -= size


def _parse_binary_hunk(
    lines: list[bytes], i: int, path: str, budget: _Budget | None
) -> tuple[BinaryHunk | None, int]:
    """One `literal` or `delta` hunk. With no budget the hunk is only read
    to its end, never inflated, and None comes back."""
    if i >= len(lines):
        raise PatchError(f"patch ends inside the binary hunk for {path}")
    match = _BINARY_RE.fullmatch(lines[i].rstrip(b"\n"))
    if not match:
        raise PatchError(f"malformed binary hunk header for {path}: {lines[i]!r}")
    kind, size = match[1].decode(), int(match[2])
    if budget is not None:
        budget.spend(size, path)
    i += 1
    deflated = bytearray()
    while i < len(lines) and lines[i] != b"\n":
        line = lines[i].rstrip(b"\n")
        count = _binary_size(line[0]) if line else 0
        encoded = line[1:]
        if not count or len(encoded) != (count + 3) // 4 * 5:
            raise PatchError(f"malformed binary hunk line for {path}")
        try:
            deflated += base64.b85decode(encoded)[:count]
        except ValueError as exc:
            raise PatchError(f"malformed binary hunk line for {path}") from exc
        i += 1
    if i >= len(lines):
        raise PatchError(f"patch ends inside the binary hunk for {path}")
    if budget is None:
        return None, i + 1
    # Inflate no further than the declared size, so a small hunk cannot
    # expand into more memory than the receiver has.
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(bytes(deflated), size + 1)
    except zlib.error as exc:
        raise PatchError(f"binary hunk for {path} does not inflate") from exc
    if len(data) != size or not inflater.eof:
        raise PatchError(f"binary hunk for {path} does not inflate to its declared size")
    if kind == "delta":
        # The delta's own header names the size of what it builds, which is
        # what applying it will hold in memory.
        _source, pos = _varint(data, 0, path)
        target, _pos = _varint(data, pos, path)
        budget.spend(target, path)
    return BinaryHunk(kind, data), i + 1


def _parse_file(lines: list[bytes], i: int, budget: _Budget) -> tuple[FileChange, int]:
    header = lines[i].rstrip(b"\n")
    names = _header_names(header[len(b"diff --git ") :])
    shown = names[1] if names else header.decode("utf-8", "replace")
    i += 1

    old_mode = new_mode = index_mode = None
    old_blob = new_blob = None
    rename_from = rename_to = None
    is_new = is_deleted = False
    while i < len(lines) and not lines[i].startswith(
        (b"diff --git ", b"--- ", b"GIT binary patch", b"Binary files ", b"@@")
    ):
        line = lines[i].rstrip(b"\n")
        if line.startswith(b"old mode "):
            old_mode = _mode(line[9:], line)
        elif line.startswith(b"new mode "):
            new_mode = _mode(line[9:], line)
        elif line.startswith(b"new file mode "):
            new_mode, is_new = _mode(line[14:], line), True
        elif line.startswith(b"deleted file mode "):
            old_mode, is_deleted = _mode(line[18:], line), True
        elif line.startswith(b"rename from "):
            rename_from = _name(line[12:], None)
        elif line.startswith(b"rename to "):
            rename_to = _name(line[10:], None)
        elif line.startswith((b"copy from ", b"copy to ")):
            raise PatchError(f"copies are not supported ({shown}); send the new file as created")
        elif line.startswith(b"index "):
            match = _INDEX_RE.fullmatch(line)
            if not match:
                raise PatchError(f"malformed index line for {shown}: {line!r}")
            old_blob, new_blob = match[1].decode(), match[2].decode()
            index_mode = match[3].decode() if match[3] else None
        elif not line.startswith(_IGNORED_HEADERS):
            raise PatchError(f"unrecognized header line for {shown}: {line!r}")
        i += 1

    minus = plus = None
    has_sides = False
    hunks: list[Hunk] = []
    binary = None
    if i < len(lines) and lines[i].startswith(b"Binary files "):
        raise PatchError(f"binary file {shown} changed without --binary")
    if i < len(lines) and lines[i].startswith(b"--- "):
        if i + 1 >= len(lines) or not lines[i + 1].startswith(b"+++ "):
            raise PatchError(f"--- line for {shown} has no +++ line after it")
        minus = _side(lines[i].rstrip(b"\n")[4:], b"a/")
        plus = _side(lines[i + 1].rstrip(b"\n")[4:], b"b/")
        has_sides = True
        i += 2
        while i < len(lines) and lines[i].startswith(b"@@"):
            hunk, i = _parse_hunk(lines, i, plus or minus or shown)
            hunks.append(hunk)
        if not hunks:
            raise PatchError(f"no hunks follow the file names for {shown}")
    elif i < len(lines) and lines[i].startswith(b"GIT binary patch"):
        binary, i = _parse_binary_hunk(lines, i + 1, shown, budget)
        # git follows the forward hunk with the reverse one, so `git apply
        # -R` works. The receiver only reads it far enough to know where
        # this file's section ends, and never inflates it.
        if i < len(lines) and lines[i].startswith((b"literal ", b"delta ")):
            _reverse, i = _parse_binary_hunk(lines, i, shown, None)
    elif i < len(lines) and lines[i].startswith(b"@@"):
        raise PatchError(f"hunk for {shown} has no --- and +++ lines")

    if i < len(lines) and not lines[i].startswith(b"diff --git "):
        raise PatchError(f"unexpected line after the changes to {shown}: {lines[i]!r}")

    old_path = None if is_new else (rename_from or (minus if has_sides else None))
    new_path = None if is_deleted else (rename_to or (plus if has_sides else None))
    if names:
        old_path = None if is_new else (old_path or names[0])
        new_path = None if is_deleted else (new_path or names[1])
        if (old_path and old_path != names[0]) or (new_path and new_path != names[1]):
            raise PatchError(f"the names for {shown} disagree between header lines")
    if has_sides and ((minus is None) != is_new or (plus is None) != is_deleted):
        raise PatchError(f"the names for {shown} disagree with its new or deleted mode")
    if has_sides and ((minus and minus != old_path) or (plus and plus != new_path)):
        raise PatchError(f"the names for {shown} disagree between header lines")
    if (old_path is None and not is_new) or (new_path is None and not is_deleted):
        raise PatchError(f"cannot tell which file {shown} changes")
    if bool(rename_from) != bool(rename_to):
        raise PatchError(f"incomplete rename for {shown}")
    if not rename_from and not is_new and not is_deleted and old_path != new_path:
        raise PatchError(f"the names for {shown} differ but it is not a rename")

    if index_mode:
        old_mode = old_mode or (None if is_new else index_mode)
        new_mode = new_mode or (None if is_deleted else index_mode)
    if old_blob is not None and set(old_blob) == {"0"}:
        old_blob = None
    if new_blob is not None and (set(new_blob) == {"0"} or is_deleted):
        new_blob = None
    if (hunks or binary) and not is_deleted and new_blob is None:
        raise PatchError(f"no index line names the new content of {shown}")
    if is_new and new_blob is None:
        raise PatchError(f"no index line names the content of new file {shown}")

    change = FileChange(
        old_path=old_path,
        new_path=new_path,
        old_mode=old_mode,
        new_mode=new_mode,
        old_blob=old_blob,
        new_blob=new_blob,
        hunks=tuple(hunks),
        binary=binary,
    )
    return change, i


def parse_patch(data: bytes, *, max_binary_bytes: int) -> tuple[FileChange, ...]:
    """Every file section of `data`, in the order git printed them.

    Raises PatchError on anything that is not plain `git diff` output:
    leading text, an unknown header, a hunk whose lines disagree with its
    counts, a binary change sent without --binary. `max_binary_bytes`
    bounds what all of the patch's binary hunks together may inflate to.
    """
    if not data:
        raise PatchError("patch is empty")
    if not data.endswith(b"\n"):
        raise PatchError("patch does not end with a newline; it may be truncated")
    lines = _split_lines(data)
    if not lines[0].startswith(b"diff --git "):
        raise PatchError("patch does not start with a diff --git line")
    budget = _Budget(max_binary_bytes)
    changes = []
    i = 0
    while i < len(lines):
        change, i = _parse_file(lines, i, budget)
        changes.append(change)
    return tuple(changes)


def _apply_hunks(base: bytes, hunks: tuple[Hunk, ...], path: str) -> bytes:
    lines = _split_lines(base)
    out: list[bytes] = []
    pos = 0
    for number, hunk in enumerate(hunks, start=1):
        # A zero count names the line before the change, not the first line of it.
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        new_start = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        old = [line for tag, line in hunk.lines if tag != b"+"]
        new = [line for tag, line in hunk.lines if tag != b"-"]
        if start < pos or len(out) + (start - pos) != new_start:
            raise PatchError(f"hunk {number} for {path} is out of order")
        out.extend(lines[pos:start])
        if lines[start : start + len(old)] != old:
            raise PatchError(
                f"hunk {number} for {path} does not match base_sha at line {hunk.old_start}"
            )
        out.extend(new)
        pos = start + len(old)
    out.extend(lines[pos:])
    return b"".join(out)


def _varint(data: bytes, pos: int, path: str) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise PatchError(f"binary delta for {path} is truncated")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _apply_delta(base: bytes, delta: bytes, path: str) -> bytes:
    """git's binary delta: a source and target size, then copy-from-base
    and insert-literal instructions."""
    source, pos = _varint(delta, 0, path)
    target, pos = _varint(delta, pos, path)
    if source != len(base):
        raise PatchError(f"binary delta for {path} does not match base_sha")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = size = 0
            for bit, shift in ((0x01, 0), (0x02, 8), (0x04, 16), (0x08, 24)):
                if op & bit:
                    offset |= delta[pos] << shift
                    pos += 1
            for bit, shift in ((0x10, 0), (0x20, 8), (0x40, 16)):
                if op & bit:
                    size |= delta[pos] << shift
                    pos += 1
            size = size or 0x10000
            if offset + size > len(base):
                raise PatchError(f"binary delta for {path} reads past the base content")
            out += base[offset : offset + size]
        elif op:
            if pos + op > len(delta):
                raise PatchError(f"binary delta for {path} is truncated")
            out += delta[pos : pos + op]
            pos += op
        else:
            raise PatchError(f"binary delta for {path} has an invalid instruction")
        if len(out) > target:
            raise PatchError(f"binary delta for {path} overruns its declared size")
    if len(out) != target:
        raise PatchError(f"binary delta for {path} does not reach its declared size")
    return bytes(out)


def apply_change(change: FileChange, base: bytes) -> bytes:
    """The new content of `change` applied to `base`, the old file's
    content (empty for a created file). Raises PatchError unless the result
    is exactly the blob the patch's index line names."""
    path = change.path
    try:
        if change.binary is None:
            result = _apply_hunks(base, change.hunks, path)
        elif change.binary.kind == "literal":
            result = change.binary.data
        else:
            result = _apply_delta(base, change.binary.data, path)
    except IndexError as exc:
        raise PatchError(f"binary delta for {path} is truncated") from exc
    if change.new_blob is None or not blob_matches(blob_sha(result), change.new_blob):
        raise PatchError(
            f"applying the patch to {path} does not reproduce blob {change.new_blob}; "
            "the patch was not made against base_sha"
        )
    return result

