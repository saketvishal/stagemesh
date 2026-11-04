"""Validate repository-local Markdown links without network access or file writes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
HTML_TAG = re.compile(r"<[^>]+>")
INLINE_MARKUP = re.compile(r"[`*_~]")


@dataclass(frozen=True)
class LinkProblem:
    source: str
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.target}: {self.reason}"


def _content_lines(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
            continue
        if fence is None:
            lines.append((number, line))
    return lines


def _slug(text: str) -> str:
    value = HTML_TAG.sub("", text).strip().casefold()
    value = INLINE_MARKUP.sub("", value)
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"\s+", "-", value)


def heading_anchors(path: Path) -> frozenset[str]:
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for _line, text in _content_lines(path):
        match = HEADING.match(text)
        if not match:
            continue
        base = _slug(match.group(2))
        index = counts.get(base, 0)
        counts[base] = index + 1
        anchors.add(base if index == 0 else f"{base}-{index}")
    return frozenset(anchors)


def _target_parts(raw: str) -> tuple[str, str] | None:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    # An optional Markdown title follows whitespace. Repository paths containing
    # literal spaces must use URL encoding or angle brackets.
    if not raw.lstrip().startswith("<"):
        target = target.split(maxsplit=1)[0]
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//"):
        return None
    return unquote(parsed.path), unquote(parsed.fragment)


def check_files(root: Path, files: list[Path]) -> list[LinkProblem]:
    root = root.resolve()
    anchors: dict[Path, frozenset[str]] = {}
    problems: list[LinkProblem] = []
    for source in files:
        source = source.resolve()
        display = source.relative_to(root).as_posix()
        for line, text in _content_lines(source):
            for match in LINK.finditer(text):
                raw = match.group(1)
                parts = _target_parts(raw)
                if parts is None:
                    continue
                target_path, fragment = parts
                candidate = source if not target_path else source.parent / target_path
                try:
                    candidate = candidate.resolve()
                    candidate.relative_to(root)
                except (OSError, ValueError):
                    problems.append(LinkProblem(display, line, raw, "target escapes repository"))
                    continue
                if not candidate.is_file():
                    problems.append(LinkProblem(display, line, raw, "target file does not exist"))
                    continue
                if fragment:
                    known = anchors.setdefault(candidate, heading_anchors(candidate))
                    if fragment.casefold() not in known:
                        problems.append(LinkProblem(display, line, raw, "heading anchor does not exist"))
    return problems


def tracked_markdown(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--", "*.md"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return [root / item for item in result.stdout.splitlines() if item]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.repo.resolve()
    files = [path if path.is_absolute() else root / path for path in args.paths]
    if not files:
        files = tracked_markdown(root)
    try:
        problems = check_files(root, files)
    except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"Documentation link check could not complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"Documentation links passed for {len(files)} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
