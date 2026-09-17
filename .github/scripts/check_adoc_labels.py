#!/usr/bin/env python3
"""Check that AsciiDoc section headers, tables, and normative blocks define an anchor label.

This script scans ``.adoc`` files for:

- Tables (delimited by ``|===`` ... ``|===``, optionally preceded by an
  attribute line such as ``[cols="3"]`` and/or a ``.Title`` block title).
- Normative blocks (``[requirement]``, ``[requirements_class]``, ``[permission]``,
  ``[recommendation]``, ``[abstract_test]``, ``[conformance_class]``).
- Section with ``sc_`` label and appropriate ``==`` title header.
- Glossary term entries nested under a "Terms and definitions" heading must use the ``def_`` prefix.

For each of these, it verifies that an anchor label (``[[label]]``) immediately
precedes it (blank lines, attribute lines, and block titles are allowed in between).
For normative blocks, the label must additionally use the expected prefix for its
type (e.g. ``req_`` for ``[requirement]``).

It also flags inconsistent ``-``/``_`` usage within a single file: if a label such as ``sc_some-class``
is already used, another label in the same file that extends it should join the extra part
with ``_`` (``sc_some-class_extra-param``), rather than using ``-`` (``sc_some-class-extra-param``),
since ``_`` separates concepts/subsections to make readability clearer.

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

# Matches the shorthand block-attribute anchor syntax, e.g.
# "[#table_implementation,reftext='...']" or "[#some-id]". This is an
# alternate, equally valid way (besides "[[id]]") to assign an id to the
# block that immediately follows.
ANCHOR_ATTR_RE = re.compile(r"^\[#([^\],\s]+)(?:\s*,.*)?\]\s*$")

# Matches a single-bracket attribute line, e.g. '[cols="3",options="header"]'
# or '[%metadata]'. Deliberately excludes anchors (double brackets) and the
# "[#id...]" shorthand anchor form.
ATTRIBUTE_RE = re.compile(r"^\[[^\[\]].*\]\s*$")

# Matches a block title line, e.g. ".Schema and Tests for the Job Status Info".
TITLE_RE = re.compile(r"^\.(\S.*)$")

# Matches a section header, capturing the "=" run and the title text.
# Level 1 ("= Title") is the document title and is intentionally excluded
# by requiring at least two leading "=" characters.
SECTION_RE = re.compile(r"^(={2,})\s+(\S.*)$")

# Matches a table delimiter line, e.g. "|===". Used for both the opening and
# closing delimiter of a table block.
TABLE_DELIM_RE = re.compile(r"^\|={3,}\s*$")

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

# Label prefixes that are disallowed for section headers (legacy/deprecated
# names that must not be reintroduced), keyed by the human-readable reason
# shown in the report.
SECTION_DISALLOWED_PREFIXES = {
    "sec_": "deprecated prefix 'sec_'; use a different, descriptive prefix (e.g. 'sc_')",
}

# Heading title that opens a "Terms and definitions" clause; every deeper-level
# heading nested under it (until a heading at the same or a shallower level is
# reached) is a glossary term entry and must use the "def_" prefix.
TERMS_AND_DEFINITIONS_RE = re.compile(r"(?i)^terms\s+and\s+definitions$")
DEFINITION_PREFIX = "def_"

# Matches an "identifier:: ..." metadata line, used to name unlabelled blocks.
IDENTIFIER_RE = re.compile(r"^identifier::\s*(\S.*)$")


@dataclass
class Finding:
    file: str
    line: int
    kind: str  # "section", "definition", "table", or the normative block type
    name: str
    label: str | None
    prefix_ok: bool
    note: str = ""
    # Labels of the enclosing section headings (outermost first), used to
    # restrict separator-consistency checks to genuine ancestors rather than
    # any same-file label that happens to share a textual prefix.
    ancestors: tuple[str, ...] = ()


def find_preceding_anchor(lines: list[str], index: int) -> tuple[str | None, int | None]:
    """Look upward from ``index`` (exclusive) for an anchor immediately attached
    to the construct starting at ``index``.

    Blank lines, attribute lines (e.g. ``[cols="3"]``), and block titles
    (e.g. ``.Caption``) are skipped over since they sit between the anchor and
    the labelled construct (section, table, or normative block).

    Returns the anchor label text (without brackets) and the line number
    (1-based) it was found on, or ``(None, None)`` if not found.
    """
    j = index - 1
    while j >= 0:
        s = lines[j].strip()
        if s == "" or TITLE_RE.match(s):
            j -= 1
            continue
        attr_anchor = ANCHOR_ATTR_RE.match(s)
        if attr_anchor:
            return attr_anchor.group(1), j + 1
        if ATTRIBUTE_RE.match(s):
            j -= 1
            continue
        break
    if j >= 0:
        m = ANCHOR_RE.match(lines[j].strip())
        if m:
            return m.group(1), j + 1
    return None, None


def find_table_title(lines: list[str], index: int) -> str | None:
    """Look upward from ``index`` (exclusive) for a block title (``.Caption``)
    attached to the table starting at ``index``, skipping blank lines,
    attribute lines, and the anchor itself.
    """
    j = index - 1
    while j >= 0:
        s = lines[j].strip()
        if s == "" or ATTRIBUTE_RE.match(s) or ANCHOR_RE.match(s):
            j -= 1
            continue
        m = TITLE_RE.match(s)
        if m:
            return m.group(1).strip()
        break
    return None


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
    in_table = False
    def_context_level: int | None = None  # heading level of an open "Terms and definitions" clause
    # Stack of (level, label) for enclosing section headings, used to derive
    # each finding's true ancestor chain (as opposed to any same-file label
    # that merely shares a textual prefix, e.g. unrelated sibling sections).
    heading_stack: list[tuple[int, str | None]] = []

    def current_ancestors() -> tuple[str, ...]:
        return tuple(lbl for _level, lbl in heading_stack if lbl)

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped == "////":
            in_comment_block = not in_comment_block
            continue
        if in_comment_block:
            continue

        if TABLE_DELIM_RE.match(stripped):
            if in_table:
                # Closing delimiter of the table opened earlier: nothing to check.
                in_table = False
                continue
            # Opening delimiter: this is the table we need a label for.
            in_table = True
            label, _ = find_preceding_anchor(lines, i)
            name = find_table_title(lines, i) or "(no title found)"
            findings.append(
                Finding(
                    file=path,
                    line=i + 1,
                    kind="table",
                    name=name,
                    label=label,
                    prefix_ok=True,
                    ancestors=current_ancestors(),
                )
            )
            continue
        if in_table:
            # Table row/content; not a candidate for section/block matching.
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            level = len(section_match.group(1))
            title = section_match.group(2).strip()

            # A heading at or above the level that opened a "Terms and
            # definitions" clause closes that clause (sibling/parent section).
            if def_context_level is not None and level <= def_context_level:
                def_context_level = None

            label, _ = find_preceding_anchor(lines, i)

            # Pop headings at the same or a deeper level: they are not
            # ancestors of this heading.
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            ancestors = current_ancestors()

            if def_context_level is not None:
                # Glossary term entry nested under "Terms and definitions".
                prefix_ok = label is not None and label.startswith(DEFINITION_PREFIX)
                findings.append(
                    Finding(
                        file=path,
                        line=i + 1,
                        kind="definition",
                        name=title,
                        label=label,
                        prefix_ok=prefix_ok,
                        note="" if prefix_ok else f"expected prefix '{DEFINITION_PREFIX}'",
                        ancestors=ancestors,
                    )
                )
                heading_stack.append((level, label))
                continue

            prefix_ok = True
            note = ""
            if label is not None:
                for bad_prefix, reason in SECTION_DISALLOWED_PREFIXES.items():
                    if label.startswith(bad_prefix):
                        prefix_ok = False
                        note = reason
                        break
            findings.append(
                Finding(
                    file=path,
                    line=i + 1,
                    kind="section",
                    name=title,
                    label=label,
                    prefix_ok=prefix_ok,
                    note=note,
                    ancestors=ancestors,
                )
            )
            heading_stack.append((level, label))
            if TERMS_AND_DEFINITIONS_RE.match(title):
                def_context_level = level
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
                    note="" if prefix_ok else f"expected prefix '{prefix}'",
                    ancestors=current_ancestors(),
                )
            )

    check_separator_consistency(findings)
    check_label_underscore_depth(findings)
    return findings


# Recognized label prefixes: only labels already following one of these
# conventions are treated as an established "root" that other labels in the
# same file might be extending (bare/legacy labels without a convention are
# not deliberate roots, and matching against them causes false positives).
RECOGNIZED_LABEL_PREFIXES = ("sc_", "ats_", "def_") + tuple(
    prefix for prefix, _ in BLOCK_TYPES.values()
)

# A trailing numeric-only suffix (e.g. "-2", "-3") is our own dedup
# disambiguation convention, not a new concept extension; it must not be
# flagged as an inconsistent "-" continuation.
_NUMERIC_SUFFIX_RE = re.compile(r"^\d+$")

# Generic descriptor words used for a subsection's own introductory/structural
# content (e.g. "sc_collection-input_access-overview"). These describe the
# established root itself rather than introducing a new concept/subsection,
# so a hyphen before them is not an inconsistency, even though the root is a
# real existing label.
GENERIC_ATTRIBUTE_SUFFIXES = {
    "overview", "operation", "response", "response-content", "request",
    "request-body", "exceptions", "error-situations", "examples",
    "sequence-diagram",
}

# A label follows a "<prefix>_<name>_<sub>" shape: at most two underscores,
# separating the prefix from the concept and the concept from a genuine
# subconcept. Any further nesting/qualification within a segment must use
# "-", not "_" (e.g. "sc_job-management_create-operation", not
# "sc_job-management_create_operation"). This caps the underscore depth.
MAX_LABEL_UNDERSCORES = 2


def check_separator_consistency(findings: list[Finding]) -> None:
    """Flag labels that extend an ancestor section's label with a hyphen
    instead of an underscore.

    Within a single file, once a label such as ``sc_job-list`` is used on a
    section, any label nested under it (e.g. ``sc_job-list-overview``) is
    expected to join the extra part with ``_`` (``sc_job-list_overview``):
    the underscore separates concepts/subsections, while hyphens are reserved
    for joining the words within a single concept. A hyphen immediately after
    an ancestor's full label is therefore an inconsistent continuation.

    Only genuine ancestors (enclosing section headings) are considered as
    candidate roots -- a same-file label that merely shares a textual prefix
    with an unrelated sibling or cousin section (e.g. "sc_openapi" and
    "sc_openapi-3-0", two distinct sibling requirements classes) is not a
    root/extension relationship and must not be flagged.
    """

    def is_exempt_suffix(suffix: str) -> bool:
        if _NUMERIC_SUFFIX_RE.match(suffix):
            return True
        # Strip a trailing "-N" dedup ordinal before comparing, e.g.
        # "overview-2" is still just the "overview" attribute.
        m = re.match(r"^(.*)-(\d+)$", suffix)
        base = m.group(1) if m else suffix
        return base in GENERIC_ATTRIBUTE_SUFFIXES

    for f in findings:
        if not f.label or not f.prefix_ok:
            continue
        labels = [
            lbl for lbl in f.ancestors
            if lbl.startswith(RECOGNIZED_LABEL_PREFIXES)
        ]
        candidates = [
            lbl for lbl in labels
            if lbl != f.label
            and f.label.startswith(lbl)
            and f.label[len(lbl)] == "-"
            and not is_exempt_suffix(f.label[len(lbl) + 1:])
            # If the root is already at the max underscore depth, joining
            # with "_" would exceed it; "-" is then the required separator,
            # not an inconsistency.
            and lbl.count("_") < MAX_LABEL_UNDERSCORES
        ]
        if not candidates:
            continue
        root = max(candidates, key=len)
        suggestion = f"{root}_{f.label[len(root) + 1:]}"
        f.prefix_ok = False
        f.note = (
            f"inconsistent separator: extends existing label '{root}' with '-'; "
            f"use '_' instead (e.g. '{suggestion}')"
        )


def check_label_underscore_depth(findings: list[Finding]) -> None:
    """Flag section/definition labels with more than
    ``MAX_LABEL_UNDERSCORES`` underscores.

    A label follows a "<prefix>_<name>_<sub>" shape: the underscore separates
    the prefix from the concept, and the concept from a genuine subconcept.
    Any further qualification must be joined with "-" instead of adding more
    underscores.
    """
    for f in findings:
        if not f.label or not f.prefix_ok:
            continue
        if f.kind not in ("section", "definition"):
            continue
        if f.label.count("_") <= MAX_LABEL_UNDERSCORES:
            continue
        prefix, rest = f.label.split("_", 1)
        parts = rest.split("_")
        suggestion = f"{prefix}_" + "_".join(parts[:MAX_LABEL_UNDERSCORES - 1]) + "_" + "-".join(parts[MAX_LABEL_UNDERSCORES - 1:])
        f.prefix_ok = False
        f.note = (
            f"label has more than {MAX_LABEL_UNDERSCORES} underscores; "
            f"join extra segments with '-' instead (e.g. '{suggestion}')"
        )


def format_kind(kind: str) -> str:
    if kind == "section":
        return "Section"
    if kind == "definition":
        return "Definition"
    if kind == "table":
        return "Table"
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
    parser.add_argument(
        "--only-errors",
        action="store_true",
        help="Only print FAIL rows (problems), omitting PASS rows from the report.",
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

    print(f"Checked {len(files)} .adoc file(s), {len(all_findings)} section header(s), "
          f"table(s), and normative block(s).\n")

    rows = []
    for f in all_findings:
        passed = f.label is not None and f.prefix_ok
        if args.only_errors and passed:
            continue
        status = "PASS" if passed else "FAIL"
        if f.label is None:
            label = "MISSING"
            note = ""
        else:
            label = f"[[{f.label}]]"
            note = f" -- {f.note}" if f.note else ""
        name = f.name if len(f.name) <= 40 else f.name[:37] + "..."
        rows.append((status, label, format_kind(f.kind), str(f.line), name, f.file + note))

    headers = ("Status", "Label", "Type", "Line", "Name", "File / Problem")
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

    if not problems:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
