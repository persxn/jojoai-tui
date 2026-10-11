"""Reading and writing text files the way their owner left them.

Shared by tools.py (local mode) and remote_exec.py (remote mode) so the two
cannot disagree about what a file contains.

── What used to go wrong ───────────────────────────────────────────────────

- **Encoding.** Everything was read as UTF-8 (before that, the locale codec).
  UTF-16 files — what Windows PowerShell 5.1's `>` and Out-File write, and
  .reg/.rc files — came back NUL-padded and could never be matched by
  edit_file; a cp1252 or latin-1 file came back full of U+FFFD, and a remote
  read-then-write replaced every accented letter with it for good. The
  encoding is now detected (BOM, then strict UTF-8, then the legacy code page)
  and every write goes back in the same one.
- **A failed write emptied the file.** open(path, "w") truncates before a
  single byte is encoded, so a lone surrogate in the new text, or a full disk,
  left an empty file behind. The bytes are now encoded first and swapped in
  atomically; the original stays intact if anything fails.
- **Line endings.** Detected per file. A file that mixes them is edited in
  place without normalising the lines the edit did not touch.
"""

from __future__ import annotations

import codecs
import locale
import os
import stat
import tempfile
from dataclasses import dataclass

_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
SNIFF = 8192


def legacy_encoding() -> str:
    """What a non-UTF-8 text file on this machine most likely is."""
    if os.name == "nt":
        enc = locale.getpreferredencoding(False) or "cp1252"
        return "cp1252" if enc.lower().replace("-", "") in ("utf8", "cp65001") else enc
    return "cp1252"


def sniff(head: bytes) -> tuple[str, bytes] | None:
    """(encoding, bom) for the first bytes of a file, or None if it is binary."""
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return enc, bom
    if b"\x00" in head:
        # UTF-16 without a BOM is rare and looks exactly like this; so does
        # every binary format. Call it binary unless every other byte is NUL.
        even, odd = head[0::2], head[1::2]
        if len(head) >= 4 and odd.count(0) > 0.9 * len(odd) and even.count(0) < 0.1 * len(even):
            return "utf-16-le", b""
        if len(head) >= 4 and even.count(0) > 0.9 * len(even) and odd.count(0) < 0.1 * len(odd):
            return "utf-16-be", b""
        return None
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        return "utf-8", b""
    except UnicodeDecodeError:
        return legacy_encoding(), b""


@dataclass
class TextFile:
    raw: str          # the text exactly as stored (original line endings)
    encoding: str     # codec to write it back with
    bom: bytes        # written back in front, if the file had one
    newline: str      # the dominant line ending: "\n" or "\r\n"
    mixed: bool       # both kinds present

    @property
    def text(self) -> str:
        """The text with "\\n" line endings."""
        return self.raw.replace("\r\n", "\n")

    def encode(self, raw: str) -> bytes:
        """Bytes for `raw` in this file's encoding. Raises ValueError, naming
        the encoding, if it cannot hold the text — before anything is written."""
        try:
            return self.bom + raw.encode(self.encoding)
        except UnicodeEncodeError as e:
            bad = raw[e.start:e.end]
            raise ValueError(f"the file is {self.encoding}, which cannot store {bad!r}") from None

    def encode_text(self, text: str) -> bytes:
        """Like encode(), from "\\n" text, converted to the file's line ending."""
        text = text.replace("\r\n", "\n")
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        return self.encode(text)


def decode(data: bytes, partial: bool = False) -> TextFile | None:
    """None if the data is binary. `partial`: `data` is the start of a longer
    file, so its last character may be cut short — drop it rather than call
    the whole thing undecodable."""
    found = sniff(data[:SNIFF])
    if found is None:
        return None
    enc, bom = found
    body = data[len(bom):]
    if partial and enc[:6] in ("utf-16", "utf-32"):
        # incrementally, so half a code unit or half a surrogate pair at the
        # cut is dropped instead of failing the whole decode
        try:
            raw = codecs.getincrementaldecoder(enc)().decode(body, final=False)
            crlf = raw.count("\r\n")
            lf = raw.count("\n") - crlf
            return TextFile(raw=raw, encoding=enc, bom=bom,
                            newline="\r\n" if crlf > lf else "\n", mixed=bool(crlf and lf))
        except UnicodeDecodeError:
            pass
    if enc == "utf-8":
        try:
            if partial:
                raw = codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
            else:
                raw = body.decode("utf-8")
        except UnicodeDecodeError:          # valid at the start, not later on
            enc = legacy_encoding()
    if enc != "utf-8":
        try:
            raw = body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            enc, raw = "latin-1", body.decode("latin-1")
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    return TextFile(raw=raw, encoding=enc, bom=bom,
                    newline="\r\n" if crlf > lf else "\n", mixed=bool(crlf and lf))


def read(path: str) -> TextFile | None:
    with open(path, "rb") as f:
        return decode(f.read())


def new_file() -> TextFile:
    return TextFile(raw="", encoding="utf-8", bom=b"", newline="\n", mixed=False)


def write_atomic(path: str, data: bytes) -> None:
    """Replace the file's contents with `data` so a failure leaves the old ones.

    Writes through symlinks (to the file they point at) and keeps permission
    bits. Falls back to an in-place write where a swap would change who owns
    the file, or the directory refuses new files.
    """
    path = os.path.realpath(path)
    d = os.path.dirname(path) or "."
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    if st is not None and not (st.st_mode & stat.S_IWUSR):
        # A swap needs only the directory to be writable, so it replaced files
        # the user had made read-only to protect them. Refuse, as open() would.
        raise PermissionError(f"{path} is read-only")
    owner_differs = st is not None and hasattr(os, "getuid") and st.st_uid != os.getuid()
    if not owner_differs:
        try:
            fd, tmp = tempfile.mkstemp(prefix=".jojo-", suffix=".tmp", dir=d)
        except OSError:
            fd = -1
        if fd >= 0:
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                # mkstemp makes the file 0600; a new file gets what open()
                # would have given it, an existing one keeps its own bits
                os.chmod(tmp, stat.S_IMODE(st.st_mode) if st is not None else 0o666 & ~_UMASK)
                if st is not None and hasattr(os, "chown") and st.st_gid != os.stat(tmp).st_gid:
                    try:
                        os.chown(tmp, -1, st.st_gid)     # keep a shared group (www-data …)
                    except OSError:
                        pass
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    pass   # Windows: target open elsewhere; write in place
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
    with open(path, "wb") as f:
        f.write(data)


def _read_umask() -> int:
    if os.name == "nt":
        return 0
    m = os.umask(0)     # the only way to read it; restored at once
    os.umask(m)
    return m


# Read once, at import: umask is process-wide, and flipping it while the
# worker threads create files would hand them the wrong mode.
_UMASK = _read_umask()
