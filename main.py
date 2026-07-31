#!/usr/bin/env python3
"""Command-line entry point for chat-ai-project-builder-and-exporter.

Turns chat-AI output (saved as a UPEP text file or read directly from
the clipboard) into a real project folder on disk.

This is a single-file build of the UPEP v2 parser: all modules
(parser, security, validator, writer, archiver, clipboard, errors) are
inlined here so the tool ships as one standalone script.

Usage
-----

::

    python3 main.py input.txt
    python3 main.py input.txt output_directory
    python3 main.py input.txt output_directory -a
    python3 main.py -c
    python3 main.py -c output_directory
    python3 main.py -c -a

Exit codes
----------
* ``0`` – success.
* ``1`` – a UPEP-specific or clipboard error occurred.
* ``2`` – a CLI usage error (missing arguments, file not found).
* ``3`` – an unexpected internal error (bug).  Includes a traceback.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import traceback
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Exit codes
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_UPEP_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_INTERNAL_ERROR = 3


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class UpepError(Exception):
    """Base class for every error raised by the UPEP parser."""


class ParseError(UpepError):
    """Raised when the textual UPEP document is malformed.

    The optional ``line`` attribute carries the 1-based line number at
    which the parser detected the problem, when available.
    """

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        if line is not None:
            super().__init__(f"Zeile {line}: {message}")
        else:
            super().__init__(message)


class ValidationError(UpepError):
    """Raised when the document is syntactically parseable but violates
    UPEP semantic rules (e.g. tree and FILE blocks disagree)."""


class SecurityError(UpepError):
    """Raised when a path would escape the project root or otherwise
    violate the safety constraints enforced by the security helpers."""


class FileSystemError(UpepError):
    """Raised when a file-system operation (creating directories,
    writing files, building the archive) fails."""


class CliError(UpepError):
    """Raised for user-facing CLI errors such as missing arguments or
    inaccessible input files."""


class ClipboardError(UpepError):
    """Raised when the system clipboard cannot be read (e.g. missing
    ``xclip`` / ``wl-paste`` on Linux, empty clipboard, unsupported
    platform)."""


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class UpepFile:
    """A single file extracted from a UPEP v2 document.

    ``path`` is the raw path as it appears after ``### FILE:`` (not yet
    validated).  ``content`` is the exact file content, including a
    trailing newline for non-empty files.  ``start_line`` is the 1-based
    line number of the ``### FILE:`` header, used for error messages.
    """

    path: str
    content: str
    start_line: int


@dataclass(frozen=True)
class UpepDocument:
    """A fully parsed UPEP v2 document.

    The ``### PROJECT TREE`` block is not stored – it is informational
    only and the parser ignores it.  ``files`` is the tuple of all
    ``### FILE:`` blocks in document order.
    """

    files: tuple[UpepFile, ...] = field(default_factory=tuple)


_FILE_MARKER_PREFIX = "### FILE:"
_END_OF_FILE_MARKER = "### END OF FILE"
_PROJECT_TREE_MARKER = "### PROJECT_TREE"
_PROJECT_TREE_MARKER_V2 = "### PROJECT TREE"

#: Maximum total input size (256 MiB).  Generous enough for any
#: realistic project export, but prevents trivial memory exhaustion.
MAX_DOCUMENT_BYTES: int = 256 * 1024 * 1024

#: Maximum number of file blocks (100 000).  Well above any real-world
#: project, prevents algorithmic-complexity attacks on the parser loop.
MAX_FILES: int = 100_000

#: Maximum content size per single file (64 MiB).  Prevents a single
#: pathological file block from consuming all memory.
MAX_FILE_CONTENT_BYTES: int = 64 * 1024 * 1024

#: UTF-8 BOM, stripped from the start of the document if present.
_UTF8_BOM = "\ufeff"


def parse_document(text: str) -> UpepDocument:
    """Parse a UPEP v2 document from its textual representation.

    The parser scans the document line by line, looking for
    ``### FILE:`` markers.  Everything else (including the
    ``### PROJECT TREE`` block and any commentary) is silently skipped.

    Parameters
    ----------
    text:
        The raw UPEP v2 document text.  Line endings (``\\n`` or
        ``\\r\\n``) are preserved by splitting on ``\\n`` only; any
        trailing ``\\r`` stays attached to the line and is later
        reproduced in file contents.  A leading UTF-8 BOM
        (``\\ufeff``) is stripped automatically.

    Returns
    -------
    UpepDocument

    Raises
    ------
    ParseError
        If the document contains no ``### FILE:`` blocks, a file
        block is malformed (e.g. missing path), the document exceeds
        ``MAX_DOCUMENT_BYTES``, or the number of file blocks exceeds
        ``MAX_FILES``.
    """
    # ------------------------------------------------------------------ #
    # 0. Size guard (defence in depth; the caller should also enforce).
    # ------------------------------------------------------------------ #
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ParseError(
            f"Dokument ist zu groß "
            f"({len(text.encode('utf-8'))} > {MAX_DOCUMENT_BYTES} Bytes)."
        )

    # Strip a leading UTF-8 BOM if present.  Many editors add one
    # silently; rejecting the document would be unhelpful.
    if text.startswith(_UTF8_BOM):
        text = text[len(_UTF8_BOM):]

    # Split on '\n' only.  This keeps a trailing '\r' on every line
    # that originally used '\r\n', which we need to faithfully
    # reproduce Windows-style line endings in extracted file contents.
    lines = text.split("\n")
    n = len(lines)
    cursor = 0

    files: list[UpepFile] = []

    while cursor < n:
        stripped = _rstrip_cr(lines[cursor]).strip()

        if stripped.startswith(_FILE_MARKER_PREFIX):
            if len(files) >= MAX_FILES:
                raise ParseError(
                    f"Dokument enthält mehr als {MAX_FILES} Datei-Blöcke.",
                    line=cursor + 1,
                )
            file_block, cursor = _parse_file_block(lines, cursor)
            files.append(file_block)
        else:
            # Commentary, PROJECT TREE, blank lines – skip silently.
            cursor += 1

    if not files:
        raise ParseError(
            "Dokument enthält keine ### FILE: Blöcke. "
            "Mindestens eine Datei muss deklariert werden."
        )

    return UpepDocument(files=tuple(files))


def _rstrip_cr(line: str) -> str:
    """Remove a single trailing ``\\r`` if present."""
    if line.endswith("\r"):
        return line[:-1]
    return line


def _parse_file_block(
    lines: list[str], cursor: int
) -> tuple[UpepFile, int]:
    """Parse a single ``### FILE:`` block starting at ``cursor``.

    A file block has the structure::

        ### FILE: <path>

        <content>
        ### END OF FILE

    The parser scans for the ``### END OF FILE`` marker.  To handle
    file contents that themselves contain the literal text
    ``### END OF FILE`` (e.g. inside a string literal or a Markdown
    document), we use a **lookahead heuristic**: a line is only treated
    as the real end marker if it is followed, after optional blank
    lines, by either EOF or another ``### FILE:`` header.

    If no such "real" end marker is found, the parser falls back to the
    *last* ``### END OF FILE`` line in the document, or – if there is
    none – to the next ``### FILE:`` marker / EOF.

    Leading and trailing blank lines around the content are stripped.
    A single trailing newline is added to non-empty files.

    Returns the parsed :class:`UpepFile` and the new cursor position.
    """
    n = len(lines)
    start_line = cursor + 1

    header = _rstrip_cr(lines[cursor])
    raw_path = header[len(_FILE_MARKER_PREFIX):].strip()
    if not raw_path:
        raise ParseError(
            "### FILE: ohne Dateipfad.",
            line=start_line,
        )
    cursor += 1

    # ------------------------------------------------------------------ #
    # Scan for the "real" ### END OF FILE marker.
    #
    # A line counts as the real end marker if:
    #   (a) it equals "### END OF FILE", AND
    #   (b) the next non-blank line is either EOF or starts with
    #       "### FILE:".
    #
    # This correctly handles file contents that contain the literal
    # text "### END OF FILE" as data (the embedded line is not the
    # real end because it is followed by further content).
    # ------------------------------------------------------------------ #
    end_idx: int | None = None
    last_end_idx: int | None = None
    scan = cursor
    while scan < n:
        stripped = _rstrip_cr(lines[scan]).strip()

        if stripped == _END_OF_FILE_MARKER:
            last_end_idx = scan
            # Lookahead: skip blank lines, check what follows.
            lookahead = scan + 1
            while lookahead < n and _rstrip_cr(lines[lookahead]).strip() == "":
                lookahead += 1
            if lookahead >= n:
                # EOF follows – this is the real end marker.
                end_idx = scan
                break
            next_stripped = _rstrip_cr(lines[lookahead]).strip()
            if next_stripped.startswith(_FILE_MARKER_PREFIX):
                # Next FILE block follows – this is the real end marker.
                end_idx = scan
                break
            # Otherwise, this ### END OF FILE line is content.
            # Keep scanning.

        elif stripped.startswith(_FILE_MARKER_PREFIX):
            # Reached the next FILE block without a real end marker.
            # Stop here; the fallback below will handle it.
            break

        scan += 1

    if end_idx is not None:
        # Normal case: real ### END OF FILE found via lookahead.
        content_lines = lines[cursor:end_idx]
        new_cursor = end_idx + 1  # Past the END OF FILE marker
    elif last_end_idx is not None:
        # Fallback A: no real end marker (content contains END OF FILE
        # lines but none is followed by EOF/next FILE).  Use the last
        # END OF FILE line as the end.
        content_lines = lines[cursor:last_end_idx]
        new_cursor = last_end_idx + 1
    else:
        # Fallback B: no ### END OF FILE at all.  Use the next
        # ### FILE: marker or EOF.
        scan = cursor
        fallback_file_idx: int | None = None
        while scan < n:
            if _rstrip_cr(lines[scan]).strip().startswith(_FILE_MARKER_PREFIX):
                fallback_file_idx = scan
                break
            scan += 1
        if fallback_file_idx is not None:
            content_lines = lines[cursor:fallback_file_idx]
            new_cursor = fallback_file_idx
        else:
            content_lines = lines[cursor:]
            new_cursor = n

    # ------------------------------------------------------------------ #
    # Strip leading and trailing blank lines from the content.
    # ------------------------------------------------------------------ #
    while content_lines and _rstrip_cr(content_lines[0]).strip() == "":
        content_lines.pop(0)
    while content_lines and _rstrip_cr(content_lines[-1]).strip() == "":
        content_lines.pop()

    content = _assemble_content(content_lines)

    # Per-file size guard.
    content_bytes_len = len(content.encode("utf-8"))
    if content_bytes_len > MAX_FILE_CONTENT_BYTES:
        raise ParseError(
            f"Datei {raw_path!r} ist zu groß "
            f"({content_bytes_len} > {MAX_FILE_CONTENT_BYTES} Bytes).",
            line=start_line,
        )

    return (
        UpepFile(
            path=raw_path,
            content=content,
            start_line=start_line,
        ),
        new_cursor,
    )


def _assemble_content(content_lines: list[str]) -> str:
    """Re-assemble the file content from the parsed content lines.

    The lines still carry their original ``\\r`` suffixes (if any), so
    joining with ``\\n`` faithfully reproduces ``\\r\\n`` line endings.

    A trailing newline is appended to every non-empty file.  This is
    the canonical convention for text files: the last line ends with
    a newline character.

    Empty files (no content lines after stripping) produce an empty
    string.
    """
    if not content_lines:
        return ""
    joined = "\n".join(content_lines)
    if not joined.endswith("\n") and not joined.endswith("\r\n"):
        joined += "\n"
    return joined


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #

#: Maximum total length of a normalized relative path (bytes in UTF-8).
#: 4096 matches the typical PATH_MAX on Linux and is generous for any
#: realistic project layout.
MAX_PATH_BYTES = 4096

#: Maximum number of path segments.  1000 is well above any real-world
#: project depth and prevents algorithmic-complexity attacks.
MAX_SEGMENTS = 1000

#: Maximum length of a single path segment (in characters).  255 matches
#: the common file-system limit (ext4, NTFS, APFS all use 255 bytes).
MAX_SEGMENT_LENGTH = 255

# Characters that must never appear in a relative path segment.  NUL is
# excluded outright because it can be used to truncate strings on some
# platforms.  We additionally reject control characters in the 0x00–0x1F
# range because they have no business being in a file name.
_FORBIDDEN_CHARS: frozenset[str] = frozenset(chr(c) for c in range(0x20))

# Reserved Windows device names.  Checked case-insensitively against the
# segment stem (the part before the first dot).
_RESERVED_WINDOWS_NAMES: frozenset[str] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def _contains_forbidden_char(segment: str) -> bool:
    """Return True if ``segment`` contains any control character."""
    return any(ch in _FORBIDDEN_CHARS for ch in segment)


def _is_reserved_windows_name(segment: str) -> bool:
    """Return True if ``segment`` is a reserved Windows device name.

    The check is case-insensitive and considers only the stem (the part
    before the first ``.``), matching Windows' own behaviour.
    """
    stem = segment.split(".", 1)[0].rstrip(" ").upper()
    return stem in _RESERVED_WINDOWS_NAMES


def validate_relative_path(raw_path: str) -> str:
    """Validate a single relative path as read from a UPEP document.

    Parameters
    ----------
    raw_path:
        The path exactly as it appears after ``### FILE:`` in the
        document.  Leading/trailing whitespace is stripped.

    Returns
    -------
    str
        The cleaned, POSIX-style relative path using ``/`` as separator.

    Raises
    ------
    SecurityError
        If the path is empty, absolute, contains a drive letter,
        traverses outside the project root, contains forbidden
        characters, exceeds length/segment limits, or uses Unicode
        lookalikes to bypass validation.
    """
    if raw_path is None:
        raise SecurityError("Pfad fehlt (None).")

    path = raw_path.strip()
    if not path:
        raise SecurityError("Leerer Dateipfad in UPEP-Dokument.")

    if "\x00" in path:
        raise SecurityError("Pfad enthält ein NUL-Byte.")

    # ------------------------------------------------------------------ #
    # Unicode normalization defense.
    #
    # Attackers may use Unicode lookalikes (e.g. fullwidth ``．．``
    # U+FF0E instead of ASCII ``.``) to bypass naive ``..`` checks.
    # On some platforms the file system normalizes these to ASCII,
    # which would then traverse.  We apply NFKC normalization *first*
    # and validate the *normalized* form.  This is the same approach
    # used by Apple's APFS and is the safe default.
    # ------------------------------------------------------------------ #
    normalized_path = unicodedata.normalize("NFKC", path)

    # Reject Windows-style drive letters and UNC paths even on POSIX
    # systems – they would be ambiguous and potentially dangerous.
    # We check on the *original* path too, because a drive letter
    # like ``C:`` survives NFKC unchanged but a fullwidth ``Ｃ：``
    # would be normalized to ``C:``.
    if len(normalized_path) >= 2 and normalized_path[1] == ":":
        raise SecurityError(
            f"Pfad enthält einen Laufwerksbuchstaben und ist nicht relativ: {path!r}"
        )
    if normalized_path.startswith("\\\\"):
        raise SecurityError(f"UNC-Pfade sind nicht erlaubt: {path!r}")

    # Normalise separators: accept both ``/`` and ``\`` in the input,
    # but always emit POSIX-style paths internally.
    normalised = normalized_path.replace("\\", "/")

    # An absolute POSIX path starts with '/'.  After the replacement
    # above a leading backslash would have been turned into a leading
    # slash, so this check covers both flavours.
    if normalised.startswith("/"):
        raise SecurityError(f"Absolute Pfade sind nicht erlaubt: {path!r}")

    # ------------------------------------------------------------------ #
    # Bounded segmentation.  We split on '/' and reject empty segments
    # (double slashes, trailing slash).  A segment count limit prevents
    # algorithmic-complexity attacks.
    # ------------------------------------------------------------------ #
    segments = normalised.split("/")
    if len(segments) > MAX_SEGMENTS:
        raise SecurityError(
            f"Pfad hat zu viele Segmente ({len(segments)} > {MAX_SEGMENTS}): {path!r}"
        )

    cleaned_segments: list[str] = []
    for segment in segments:
        if segment == "":
            raise SecurityError(
                f"Pfad enthält ein leeres Segment (Doppelslash oder "
                f"abschließenden Slash): {path!r}"
            )

        if len(segment) > MAX_SEGMENT_LENGTH:
            raise SecurityError(
                f"Pfad-Segment ist zu lang ({len(segment)} > "
                f"{MAX_SEGMENT_LENGTH} Zeichen): {path!r}"
            )

        if segment == ".":
            # ``./foo`` is semantically harmless, but we strip it to get
            # a canonical representation.
            continue

        if segment == "..":
            raise SecurityError(
                f"Pfad enthält ein '..'-Segment (Path-Traversal-Versuch): {path!r}"
            )

        if _contains_forbidden_char(segment):
            raise SecurityError(
                f"Pfad enthält ungültige Steuerzeichen: {path!r}"
            )

        if _is_reserved_windows_name(segment):
            raise SecurityError(
                f"Pfad verwendet einen reservierten Dateinamen: {path!r}"
            )

        cleaned_segments.append(segment)

    if not cleaned_segments:
        raise SecurityError(
            f"Pfad reduziert sich zu nichts nach Bereinigung: {path!r}"
        )

    result = "/".join(cleaned_segments)

    # Final byte-length check on the canonical form.
    result_bytes = result.encode("utf-8")
    if len(result_bytes) > MAX_PATH_BYTES:
        raise SecurityError(
            f"Pfad ist zu lang ({len(result_bytes)} > {MAX_PATH_BYTES} Bytes): {path!r}"
        )

    return result


def resolve_within_root(root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` against ``root`` and ensure the result
    stays inside ``root``.

    The function performs the following steps:

    1. Validates ``relative_path`` with :func:`validate_relative_path`.
    2. Resolves ``root`` to an absolute, symlink-free path.
    3. Joins the relative path and resolves it.
    4. Verifies that the resolved path is either equal to ``root`` or
       has ``root`` as one of its parents.

    Parameters
    ----------
    root:
        The destination directory the user chose on the command line.
        The directory does not need to exist yet.
    relative_path:
        A path relative to ``root``, typically the validated path of a
        UPEP ``FILE`` block.

    Returns
    -------
    Path
        The absolute, resolved target path.

    Raises
    ------
    SecurityError
        If the resolved target escapes ``root`` for any reason.
    """
    safe_relative = validate_relative_path(relative_path)

    # ``Path.resolve(strict=False)`` does not require the path to exist,
    # which is what we want because we are about to create it.
    root_resolved = root.resolve(strict=False)

    # Build the candidate path and resolve it.  ``resolve()`` expands
    # symlinks, so if any intermediate component is a symlink pointing
    # outside the root, the resolved path will escape and the
    # containment check below will catch it.
    candidate = (root_resolved / safe_relative).resolve(strict=False)

    # Containment check: the resolved root must be a prefix of the
    # resolved candidate.
    if not _is_within(candidate, root_resolved):
        raise SecurityError(
            f"Pfad verlässt das Zielverzeichnis: {relative_path!r} -> {candidate}"
        )

    return candidate


def _is_within(child: Path, parent: Path) -> bool:
    """Return True iff ``child`` is equal to or located inside ``parent``.

    Both paths must already be resolved.  Uses ``Path.is_relative_to``
    (Python 3.9+) with a defensive fallback.
    """
    if hasattr(child, "is_relative_to"):
        try:
            return child.is_relative_to(parent)
        except ValueError:
            return False
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def is_symlink_safe(path: Path, root: Path) -> bool:
    """Return True iff ``path`` is not a symlink, or is a symlink that
    resolves *inside* ``root``.

    This is used by the writer to refuse writing through symlinks that
    point outside the project root, which would bypass the containment
    check at write time (TOCTOU: the path was safe at validation time
    but a symlink was created/changed before the write).
    """
    if not path.is_symlink():
        return True
    target = path.resolve(strict=False)
    return _is_within(target, root.resolve(strict=False))


def ensure_directory(path: Path) -> None:
    """Create ``path`` (and any missing parents) with mode 0o777 minus
    the current umask.

    The function is a thin wrapper around :meth:`Path.mkdir` that
    treats the "directory already exists" case as success.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileSystemError(
            f"Verzeichnis konnte nicht erstellt werden: {path} ({exc})"
        ) from exc


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #

def validate_document(document: UpepDocument) -> list[str]:
    """Validate a parsed UPEP v2 document and return the canonical list
    of file paths.

    The returned list is in **document order** (the order in which the
    ``### FILE:`` blocks appear in the document).  This is the order in
    which files are written to disk.

    Parameters
    ----------
    document:
        The parsed document to validate.

    Returns
    -------
    list[str]
        The validated, document-ordered list of file paths.

    Raises
    ------
    ValidationError
        If any check fails.
    """
    if not document.files:
        raise ValidationError("Dokument enthält keinen FILE-Block.")

    # ------------------------------------------------------------------ #
    # Validate each file's path individually (security + syntax) and
    # check for duplicates.
    # ------------------------------------------------------------------ #
    seen_paths: dict[str, int] = {}  # path -> first occurrence line
    validated_paths: list[str] = []

    for block in document.files:
        try:
            safe_path = validate_relative_path(block.path)
        except (ValidationError, SecurityError) as exc:
            raise ValidationError(
                f"Datei {block.path!r} (Zeile {block.start_line}): {exc}"
            ) from exc

        if safe_path in seen_paths:
            raise ValidationError(
                f"Doppelter Dateipfad: {block.path!r} "
                f"(zuerst Zeile {seen_paths[safe_path]}, "
                f"dann Zeile {block.start_line})."
            )
        seen_paths[safe_path] = block.start_line
        validated_paths.append(safe_path)

    return validated_paths


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class WriteResult:
    """Summary of a write operation."""

    files_written: int
    directories_created: int
    bytes_written: int
    project_root: Path


def write_document(
    document: UpepDocument,
    output_dir: Path,
    *,
    overwrite: bool = True,
) -> WriteResult:
    """Write every file declared in ``document`` under ``output_dir``.

    Parameters
    ----------
    document:
        A parsed (but not yet validated) UPEP v2 document.  The
        function validates it first; a :class:`ValidationError`
        propagates unchanged.
    output_dir:
        The destination directory.  Created if missing.  May already
        exist.
    overwrite:
        If True (default), existing files at the target paths are
        overwritten.  If False, a :class:`FileSystemError` is raised
        when a target file already exists.

    Returns
    -------
    WriteResult
        Counts of written files / directories / bytes plus the
        resolved project root.

    Raises
    ------
    ValidationError
        If the document is semantically invalid.
    SecurityError
        If any resolved path escapes ``output_dir`` or a symlink
        would be followed outside the root.
    FileSystemError
        If a file-system operation fails (permissions, disk full,
        etc.).
    """
    # Validate first – this gives us the canonical, document-ordered
    # list of file paths and rejects any document that doesn't make
    # sense.
    validated_paths = validate_document(document)

    # Build a mapping from validated path -> file block.  The validator
    # has already checked for duplicates, so the mapping is unique.
    path_to_block = {
        validate_relative_path(block.path): block for block in document.files
    }

    # Resolve the output directory once.
    root_resolved = output_dir.resolve(strict=False)
    ensure_directory(root_resolved)

    files_written = 0
    directories_created = 0
    bytes_written = 0
    created_dirs: set[Path] = set()

    for relative_path in validated_paths:
        block = path_to_block[relative_path]

        # Re-validate immediately before writing (TOCTOU defence).
        target = resolve_within_root(root_resolved, relative_path)

        # Make sure the parent directory exists.
        parent = target.parent
        if parent not in created_dirs and not parent.exists():
            to_create: list[Path] = []
            cursor_dir = parent
            while cursor_dir != root_resolved and not cursor_dir.exists():
                to_create.append(cursor_dir)
                cursor_dir = cursor_dir.parent
            ensure_directory(parent)
            created_dirs.update(to_create)
            created_dirs.add(parent)
            directories_created += len(to_create)

        # Refuse to overwrite an existing symlink that escapes the root.
        if target.is_symlink() and not is_symlink_safe(target, root_resolved):
            raise SecurityError(
                f"Weigere mich, durch einen Symlink zu schreiben, der "
                f"das Zielverzeichnis verlässt: {target}"
            )

        if not overwrite and target.exists():
            raise FileSystemError(
                f"Datei existiert bereits und overwrite=False: {target}"
            )

        _write_file_atomic(target, block.content)

        files_written += 1
        bytes_written += len(block.content.encode("utf-8"))

    return WriteResult(
        files_written=files_written,
        directories_created=directories_created,
        bytes_written=bytes_written,
        project_root=root_resolved,
    )


def _write_file_atomic(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically.

    Uses :func:`tempfile.mkstemp` to create an unpredictable temp file
    name in the target's parent directory, then :func:`os.replace` to
    atomically rename it.  This prevents symlink races where an
    attacker pre-creates a symlink at the predictable ``.upep-tmp``
    name to divert writes.

    The temp file is created with restrictive permissions (0600) by
    :func:`tempfile.mkstemp` and inherits the parent directory's
    ownership.
    """
    parent = target.parent
    payload = content.encode("utf-8")

    # mkstemp returns (fd, path).  The file is opened with O_EXCL,
    # so it cannot exist yet (no symlink race).
    fd: int
    tmp_path: str
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(parent),
            prefix=f".{target.name}.",
            suffix=".upep-tmp",
        )
    except OSError as exc:
        raise FileSystemError(
            f"Konnte Temp-Datei nicht erstellen in {parent}: {exc}"
        ) from exc

    try:
        # Write the payload.
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        except OSError as exc:
            raise FileSystemError(
                f"Konnte Datei nicht schreiben: {target} ({exc})"
            ) from exc

        # Atomically replace the target.
        try:
            os.replace(tmp_path, target)
        except OSError as exc:
            raise FileSystemError(
                f"Konnte Datei nicht atomar ersetzen: {target} ({exc})"
            ) from exc
    except BaseException:
        # Clean up the temp file on any failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Archiver
# --------------------------------------------------------------------------- #

def create_archive(
    project_root: Path,
    archive_path: Path | None = None,
    *,
    overwrite: bool = True,
) -> Path:
    """Create a ZIP archive of ``project_root``.

    Parameters
    ----------
    project_root:
        The directory to archive.  Must exist.
    archive_path:
        Optional explicit path for the resulting ``.zip`` file.  If
        omitted, the archive is created next to ``project_root`` with
        the same base name plus ``.zip``.
    overwrite:
        If True (default), an existing ZIP archive at ``archive_path``
        is overwritten.  If False, a :class:`FileSystemError` is raised
        when the target file already exists.  In both cases, a non-ZIP
        file at the target path is rejected to prevent silent data
        loss.

    Returns
    -------
    Path
        The resolved path of the created archive.

    Raises
    ------
    FileSystemError
        If the project root does not exist, the archive cannot be
        written, or a non-ZIP file exists at the target path.
    SecurityError
        If an archive entry would escape the project root (ZIP-slip).
    """
    if not project_root.exists() or not project_root.is_dir():
        raise FileSystemError(
            f"Projektverzeichnis existiert nicht: {project_root}"
        )

    if archive_path is None:
        archive_path = project_root.with_suffix(project_root.suffix + ".zip")
    archive_path = archive_path.resolve(strict=False)

    # ------------------------------------------------------------------ #
    # Guard against silent data loss: if the target file exists but is
    # not a ZIP archive, refuse to overwrite it.  This prevents the
    # archiver from destroying user data.
    # ------------------------------------------------------------------ #
    if archive_path.exists():
        if not overwrite:
            raise FileSystemError(
                f"Archiv-Zieldatei existiert bereits und overwrite=False: {archive_path}"
            )
        if not zipfile.is_zipfile(archive_path):
            raise FileSystemError(
                f"Archiv-Zieldatei existiert, ist aber kein ZIP-Archiv "
                f"(verweigere Überschreiben zum Schutz der Daten): {archive_path}"
            )

    # The directory name inside the archive.  We use the resolved
    # project root's name so that extracting the archive reproduces the
    # project directory.
    root_resolved = project_root.resolve(strict=False)
    root_name = root_resolved.name

    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as zf:
            for dirpath, dirnames, filenames in os.walk(project_root):
                # Sort for deterministic archive contents.
                dirnames.sort()
                filenames.sort()

                rel_dir = Path(dirpath).relative_to(project_root)

                # Add directories explicitly so that empty directories
                # are preserved.
                if str(rel_dir) != ".":
                    arc_dir = Path(root_name) / rel_dir
                    _write_dir_entry(zf, arc_dir)

                for filename in filenames:
                    abs_file = Path(dirpath) / filename
                    rel_file = rel_dir / filename
                    arc_name = Path(root_name) / rel_file

                    # Skip symlinks that escape the root (ZIP-slip
                    # defence on the *writing* side).
                    if abs_file.is_symlink():
                        link_target = abs_file.resolve(strict=False)
                        if not _is_within(link_target, root_resolved):
                            # Skip this entry – it would point outside
                            # the archive's logical root.
                            continue

                    _write_file_entry(zf, abs_file, arc_name)

    except OSError as exc:
        raise FileSystemError(
            f"Konnte ZIP-Archiv nicht erstellen: {archive_path} ({exc})"
        ) from exc

    return archive_path


def _write_dir_entry(zf: zipfile.ZipFile, arc_dir: Path) -> None:
    """Add a directory entry to the archive, validating the name."""
    arc_name = str(arc_dir).replace("\\", "/") + "/"
    _validate_arc_name(arc_name)
    zf.mkdir(arc_name)


def _write_file_entry(zf: zipfile.ZipFile, abs_file: Path, arc_name: Path) -> None:
    """Add a file entry to the archive, validating the name."""
    name = str(arc_name).replace("\\", "/")
    _validate_arc_name(name)
    zf.write(abs_file, name)


def _validate_arc_name(arc_name: str) -> None:
    """Validate that an archive entry name is safe.

    Rejects absolute paths and any path containing ``..`` segments.
    This is the writing-side defence against ZIP-slip: even if a
    malicious path somehow reached the archiver, it would be rejected
    here.
    """
    if arc_name.startswith("/"):
        raise SecurityError(
            f"Archiveintrag ist absolut (ZIP-Slip-Versuch): {arc_name!r}"
        )
    parts = arc_name.replace("\\", "/").split("/")
    for part in parts:
        if part == "..":
            raise SecurityError(
                f"Archiveintrag enthält '..' (ZIP-Slip-Versuch): {arc_name!r}"
            )


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #

def get_clipboard_text() -> str:
    """Return the current text content of the system clipboard.

    Returns
    -------
    str
        The clipboard text.  May be empty if the clipboard contains no
        text.

    Raises
    ------
    ClipboardError
        If the clipboard cannot be read (missing tool on Linux, empty
        clipboard, unsupported platform, etc.).
    """
    system = platform.system()

    if system == "Windows":
        return _get_clipboard_windows()
    if system == "Darwin":
        return _get_clipboard_macos()
    if system == "Linux":
        return _get_clipboard_linux()
    raise ClipboardError(f"Unsupported operating system: {system}")


def _get_clipboard_windows() -> str:
    """Read the clipboard on Windows via the WinAPI (ctypes)."""
    CF_UNICODETEXT = 13

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    if not user32.OpenClipboard(None):
        raise ClipboardError("Could not open the clipboard.")

    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            raise ClipboardError("No text in the clipboard.")

        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            raise ClipboardError("Could not read clipboard data.")

        kernel32.GlobalLock.restype = ctypes.c_void_p
        pointer = kernel32.GlobalLock(h_data)
        if not pointer:
            raise ClipboardError("Could not lock clipboard memory.")

        try:
            text = ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(h_data)

        return text
    finally:
        user32.CloseClipboard()


def _get_clipboard_macos() -> str:
    """Read the clipboard on macOS via ``pbpaste``."""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ClipboardError(
            "'pbpaste' not found (should ship with macOS)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ClipboardError(f"'pbpaste' failed: {exc}") from exc

    return result.stdout


def _is_wayland() -> bool:
    """Return True if the current session is Wayland."""
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _get_clipboard_linux() -> str:
    """Dispatch to the Wayland or X11 reader depending on the session."""
    if _is_wayland():
        return _get_clipboard_wayland()
    return _get_clipboard_x11()


def _get_clipboard_wayland() -> str:
    """Read the clipboard on Wayland via ``wl-paste``."""
    if shutil.which("wl-paste") is None:
        raise ClipboardError(
            "Wayland session detected, but 'wl-paste' was not found.\n"
            "Install it, e.g.:  sudo apt install wl-clipboard"
        )
    try:
        result = subprocess.run(
            ["wl-paste", "--no-newline"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ClipboardError(f"'wl-paste' failed: {exc}") from exc

    return result.stdout


def _get_clipboard_x11() -> str:
    """Read the clipboard on X11 via ``xclip`` (preferred) or ``xsel``."""
    if shutil.which("xclip"):
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as exc:
            raise ClipboardError(f"'xclip' failed: {exc}") from exc

    if shutil.which("xsel"):
        try:
            result = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as exc:
            raise ClipboardError(f"'xsel' failed: {exc}") from exc

    raise ClipboardError(
        "X11 session detected, but neither 'xclip' nor 'xsel' was found.\n"
        "Install one of them, e.g.:  sudo apt install xclip"
    )


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the chat-ai-project-builder-and-exporter CLI."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Turn chat-AI output into real project files on disk. "
            "Reads a UPEP text file (or the clipboard) and reconstructs "
            "the full project structure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 main.py input.txt                 reconstruct from file\n"
            "  python3 main.py input.txt myproject/      reconstruct into myproject/\n"
            "  python3 main.py input.txt myproject/ -a   also create a ZIP\n"
            "  python3 main.py -c                        read input from clipboard\n"
            "  python3 main.py -c myproject/ -a          clipboard -> folder + ZIP\n"
        ),
    )
    parser.add_argument(
        "input",
        type=str,
        nargs="?",
        default=None,
        help=(
            "Path to the UPEP text file produced by the chat AI. "
            "Optional if --clipboard is used."
        ),
    )
    parser.add_argument(
        "output",
        type=str,
        nargs="?",
        default=None,
        help=(
            "Output directory. If omitted, the project is created in "
            "the current working directory."
        ),
    )
    parser.add_argument(
        "-c",
        "--clipboard",
        action="store_true",
        help=(
            "Read the UPEP input from the system clipboard instead of a "
            "file. Supports Windows, macOS, Linux/X11 (xclip/xsel) and "
            "Linux/Wayland (wl-paste)."
        ),
    )
    parser.add_argument(
        "-a",
        "--archive",
        action="store_true",
        help="Also create a ZIP archive of the reconstructed project.",
    )
    return parser


# --------------------------------------------------------------------------- #
# Core command logic
# --------------------------------------------------------------------------- #

def run_command(args: argparse.Namespace) -> int:
    """Execute the parsed command.  Returns an exit code."""
    # ------------------------------------------------------------------ #
    # 1. Determine the input source (file or clipboard).
    #
    # Because both ``input`` and ``output`` are optional positional
    # arguments, argparse fills them left-to-right.  When ``-c`` is
    # used, the first positional is actually the output directory, so
    # we shift the values accordingly.
    # ------------------------------------------------------------------ #
    if args.clipboard:
        # With -c, there is no input file.  The positional arguments
        # (if any) are the output directory.
        if args.output is not None:
            # Two positionals were given with -c — that's too many.
            raise CliError(
                "With --clipboard, at most one positional argument "
                "(the output directory) is allowed."
            )
        output_from_positional = args.input
        args.input = None
        if output_from_positional is not None:
            args.output = output_from_positional
    else:
        if args.input is None:
            raise CliError(
                "No input source given. Provide an input file or use --clipboard."
            )

    # ------------------------------------------------------------------ #
    # 2. Read the input text.
    # ------------------------------------------------------------------ #
    if args.clipboard:
        print("Reading input from clipboard ...")
        try:
            text = get_clipboard_text()
        except ClipboardError as exc:
            raise ClipboardError(f"Could not read clipboard: {exc}") from exc
        source_label = "<clipboard>"
    else:
        input_path = Path(args.input).expanduser()
        if not input_path.exists():
            raise CliError(f"Input file does not exist: {input_path}")
        if not input_path.is_file():
            raise CliError(f"Input path is not a file: {input_path}")

        try:
            raw_bytes = input_path.read_bytes()
        except OSError as exc:
            raise CliError(
                f"Could not read input file: {input_path} ({exc})"
            ) from exc

        # Decode as UTF-8.  We reject files that are not valid UTF-8
        # outright – the UPEP spec mandates Unicode support.
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CliError(
                f"Input file is not valid UTF-8: {input_path} ({exc})"
            ) from exc
        source_label = str(input_path)

    # ------------------------------------------------------------------ #
    # 3. Determine the output directory.
    # ------------------------------------------------------------------ #
    if args.output is None:
        output_dir = Path.cwd()
    else:
        output_dir = Path(args.output).expanduser()

    # ------------------------------------------------------------------ #
    # 4. Parse and write.
    # ------------------------------------------------------------------ #
    print(f"Parsing UPEP document: {source_label}")
    document = parse_document(text)
    print(f"  {len(document.files)} file(s) declared.")

    print(f"Writing project to: {output_dir}")
    result = write_document(document, output_dir)
    print(
        f"  {result.files_written} file(s) written, "
        f"{result.directories_created} folder(s) created, "
        f"{result.bytes_written} bytes."
    )

    # ------------------------------------------------------------------ #
    # 5. Optional ZIP archive.
    # ------------------------------------------------------------------ #
    if args.archive:
        print("Creating ZIP archive ...")
        archive_path = create_archive(result.project_root)
        print(f"  Archive created: {archive_path}")

    print("Done.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    """Program entry point.  Returns an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return run_command(args)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except UpepError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_UPEP_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return EXIT_UPEP_ERROR
    except Exception as exc:  # noqa: BLE001  (top-level safety net)
        print(
            f"Internal error: {exc}\n\n"
            "This is a bug. Please report it with the following "
            "traceback:\n",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

__version__ = "2.1.0"

__all__ = [
    "__version__",
    # Parser
    "parse_document",
    "UpepDocument",
    "UpepFile",
    "MAX_DOCUMENT_BYTES",
    "MAX_FILES",
    "MAX_FILE_CONTENT_BYTES",
    # Validation
    "validate_document",
    # Security
    "validate_relative_path",
    "resolve_within_root",
    "is_symlink_safe",
    "ensure_directory",
    "MAX_PATH_BYTES",
    "MAX_SEGMENTS",
    "MAX_SEGMENT_LENGTH",
    # Writer
    "write_document",
    "WriteResult",
    # Archiver
    "create_archive",
    # Clipboard
    "get_clipboard_text",
    # Errors
    "UpepError",
    "ParseError",
    "ValidationError",
    "SecurityError",
    "FileSystemError",
    "CliError",
    "ClipboardError",
]


if __name__ == "__main__":
    sys.exit(main())
