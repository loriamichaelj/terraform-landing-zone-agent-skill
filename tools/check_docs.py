#!/usr/bin/env python3
"""Checks the repo's markdown: every relative link (and image) resolves to a file in the repo,
and every #anchor to a heading.

External links are not fetched: they make CI flaky and say nothing about this repo.

Usage: check_docs.py [repo root]        Exit codes: 0 clean, 1 findings, 2 usage error. Stdlib only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text.strip())


def lines_outside_fences(text: str):
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def anchors(path: Path, cache: dict[Path, set[str]]) -> set[str]:
    if path not in cache:
        seen: dict[str, int] = {}
        found: set[str] = set()
        for _, line in lines_outside_fences(path.read_text(encoding="utf-8")):
            match = HEADING.match(line)
            if match:
                base = slug(match.group("text"))
                count = seen.get(base, 0)
                seen[base] = count + 1
                found.add(base if count == 0 else f"{base}-{count}")
        cache[path] = found
    return cache[path]


def markdown_files(root: Path) -> list[Path]:
    """The repo's own markdown. Vendored upstream trees are verbatim third-party files whose links point into
    the upstream repo, and they must not be edited, so they are not checked."""
    try:
        out = subprocess.run(["git", "ls-files", "*.md"], cwd=root, capture_output=True, text=True, check=True).stdout
        found = [root / line for line in out.splitlines() if (root / line).is_file()]
    except (OSError, subprocess.CalledProcessError):
        found = [p for p in root.rglob("*.md") if ".git" not in p.parts]
    return sorted(p for p in found if "upstream" not in p.relative_to(root).parts)


def check_links(root: Path, files: list[Path]) -> list[str]:
    findings: list[str] = []
    cache: dict[Path, set[str]] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        for number, line in lines_outside_fences(path.read_text(encoding="utf-8")):
            line = re.sub(r"`[^`]*`", "", line)  # inline code is not a link
            for match in LINK.finditer(line):
                target = match.group("target")
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("//"):
                    continue  # http, https, mailto and other external schemes
                file_part, _, fragment = target.partition("#")
                destination = path if not file_part else (path.parent / unquote(file_part)).resolve()
                where = f"{relative}:{number}"
                if not destination.is_relative_to(root.resolve()) or not destination.exists():
                    findings.append(f"{where}: link target not found: {target}")
                elif fragment and destination.suffix == ".md" and unquote(fragment).lower() not in anchors(destination, cache):
                    findings.append(f"{where}: no heading for anchor: {target}")
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[0] if argv else ".").resolve()
    if not root.is_dir():
        print(f"check_docs: {root} is not a directory", file=sys.stderr)
        return 2
    files = markdown_files(root)
    findings = check_links(root, files)
    for message in findings:
        print(message)
    if not findings:
        print(f"{len(files)} markdown file(s): links and anchors resolve")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
