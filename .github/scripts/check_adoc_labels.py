#!/usr/bin/env python3
"""Check that AsciiDoc section headers and normative blocks define an anchor label.

This script scans ``.adoc`` files for:

- Section headers (``==``, ``===``, ...; the top-level document title ``=`` is
  excluded since it is never labelled in this repository).
- Normative blocks (``[requirement]``, ``[requirements_class]``, ``[permission]``,
  ``[recommendation]``, ``[abstract_test]``, ``[conformance_class]``).

For each of these, it verifies that an anchor label (``[[label]]``) immediately
precedes it (blank lines are allowed in between). For normative blocks, the
label must additionally use the expected prefix for its type
(e.g. ``req_`` for ``[requirement]``).

Usage::

    python check_adoc_labels.py                         # recursively check the current directory
    python check_adoc_labels.py DIR                     # recursively check the specified directory
    python check_adoc_labels.py -f FILE [-f FILE ...]   # check the specific file(s)

Exits with a non-zero status if any missing or incorrectly prefixed label is
found, so it can be used as a CI gate.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Matches an anchor label on its own line, e.g. "[[req_core_get-op]]".
ANCHOR_RE = re.compile(r"^\[\[([^\]]+)\]\]\s*$")

# Matches a section header, capturing the "=" run and the title text.
# Level 1 ("= Title") is the document title and is intentionally excluded
# by requiring at least two leading "=" characters.
SECTION_RE = re.compile(r"^(={2,})\s+(\S.*)$")

# Matches a normative block marker, e.g. "[requirement]".
BLOCK_RE = re.compile(r"^\[([a-z_]+)\]\s*$")

# Maps normative block type -> (expected label prefix, human readable name).
BLOCK_TYPES = {
    "requirement": ("req_", "Requirement"),
    "requirements_class": ("rc_", "Requirement Class"),
    "permission": ("per_", "Permission"),
    "recommendation": ("rec_", "Recommendation"),
    "abstract_test": ("ats_", "Abstract Test"),
    "conformance_class": ("ats_", "Conformance Class"),
}

# Matches an "identifier:: ..." metadata line, used to name unlabelled blocks.
IDENTIFIER_RE = re.compile(r"^identifier::\s*(\S.*)$")


@dataclass
class Finding:
    file: str
    line: int
    kind: str  # "section" or the normative block type
    name: str
    label: str | None
    prefix_ok: bool


def find_preceding_anchor(lines: list[str], index: int) -> tuple[str | None, int | None]:
    """Look upward from ``index`` (exclusive), skipping blank lines, for an anchor.

    Returns the anchor label text (without brackets) and the line number
    (1-based) it was found on, or ``(None, None)`` if not found.
    """
    j = index - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j >= 0:
        m = ANCHOR_RE.match(lines[j].strip())
        if m:
            return m.group(1), j + 1
    return None, None


def find_identifier(lines: list[str], start_index: int, limit: int = 12) -> str | None:
    """Look downward from ``start_index`` for an "identifier::" metadata line."""
    for k in range(start_index, min(start_index + limit, len(lines))):
        m = IDENTIFIER_RE.match(lines[k].strip())
        if m:
            return m.group(1).strip()
    return None


def check_file(path: str) -> list[Finding]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    findings: list[Finding] = []
    in_comment_block = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped == "////":
            in_comment_block = not in_comment_block
            continue
        if in_comment_block:
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            label, _ = find_preceding_anchor(lines, i)
            findings.append(
                Finding(
                    file=path,
                    line=i + 1,
                    kind="section",
                    name=section_match.group(2).strip(),
                    label=label,
                    prefix_ok=True,
                )
            )
            continue

        block_match = BLOCK_RE.match(stripped)
        if block_match and block_match.group(1) in BLOCK_TYPES:
            block_type = block_match.group(1)
            prefix, _readable = BLOCK_TYPES[block_type]
            label, _ = find_preceding_anchor(lines, i)
            name = find_identifier(lines, i + 1) or "(no identifier found)"
            prefix_ok = label is not None and label.startswith(prefix)
            findings.append(
                Finding(
                    file=path,
                    line=i + 1,
                    kind=block_type,
                    name=name,
                    label=label,
                    prefix_ok=prefix_ok,
                )
            )

    return findings


def format_kind(kind: str) -> str:
    if kind == "section":
        return "Section"
    return BLOCK_TYPES[kind][1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        metavar="DIR",
        help="Directory to recursively scan for .adoc files (default: current "
        "directory). Ignored if -f/--file is used.",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        default=[],
        metavar="FILE",
        help="Adoc file to check. May be given multiple times. "
        "If omitted, recursively check every .adoc file under DIR.",
    )
    args = parser.parse_args(argv)

    if args.files:
        files = [f for f in args.files if f.endswith(".adoc")]
    else:
        root = Path(args.directory)
        if not root.is_dir():
            parser.error(f"not a directory: {args.directory}")
        files = [str(p) for p in sorted(root.rglob("*.adoc"))]

    if not files:
        print("No .adoc files to check.")
        return 0

    all_findings: list[Finding] = []
    for path in files:
        all_findings.extend(check_file(path))

    problems = [f for f in all_findings if f.label is None or not f.prefix_ok]

    if not problems:
        print(f"Checked {len(files)} .adoc file(s): all section headers and normative "
              f"blocks have valid anchor labels.")
        return 0

    print("The following section headers and/or normative blocks are missing a valid "
          "anchor label ([[label]]):\n")

    rows = []
    for f in problems:
        if f.label is None:
            status = "MISSING"
            note = ""
        else:
            expected_prefix = BLOCK_TYPES[f.kind][0] if f.kind in BLOCK_TYPES else ""
            status = f"[[{f.label}]]"
            note = f" -- expected prefix '{expected_prefix}'"
        rows.append((status, format_kind(f.kind), str(f.line), f.name[:40], f.file + note))

    headers = ("Label", "Type", "Line", "Name", "File")
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers) - 1)  # File column is left unpadded (last column)
    ]

    def format_row(cols: tuple[str, ...]) -> str:
        padded = [cols[i].ljust(widths[i]) for i in range(len(widths))]
        return " ".join(padded) + " " + cols[-1]

    header_line = format_row(headers)
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(format_row(row))

    print(f"\n{len(problems)} problem(s) found across {len(files)} file(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
