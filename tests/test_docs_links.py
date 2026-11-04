from __future__ import annotations

from pathlib import Path

from scripts.check_docs_links import check_files, heading_anchors, main, tracked_markdown

ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_relative_files_anchors_external_links_and_fenced_examples(tmp_path: Path) -> None:
    guide = write(
        tmp_path / "docs" / "guide.md",
        "# Guide\n\n## Repeated\n\n## Repeated\n\n### Café safety\n",
    )
    readme = write(
        tmp_path / "README.md",
        "# Root\n\n[guide](docs/guide.md#repeated-1)\n"
        "[unicode](docs/guide.md#café-safety)\n[web](https://example.com/x)\n"
        "```md\n[example](missing.md)\n```\n",
    )

    assert heading_anchors(guide) >= {"guide", "repeated", "repeated-1", "café-safety"}
    assert check_files(tmp_path, [readme, guide]) == []


def test_missing_file_missing_anchor_and_repository_escape_are_reported(tmp_path: Path) -> None:
    guide = write(tmp_path / "guide.md", "# Existing\n")
    readme = write(
        tmp_path / "README.md",
        "[missing](missing.md)\n[anchor](guide.md#absent)\n[escape](../outside.md)\n",
    )

    problems = check_files(tmp_path, [readme, guide])

    assert [(item.line, item.reason) for item in problems] == [
        (1, "target file does not exist"),
        (2, "heading anchor does not exist"),
        (3, "target escapes repository"),
    ]


def test_cli_reports_source_line_and_target_without_modifying_files(
    tmp_path: Path, capsys
) -> None:
    readme = write(tmp_path / "README.md", "[missing](docs/nope.md)\n")
    before = readme.read_bytes()

    assert main(["--repo", str(tmp_path), "README.md"]) == 1

    assert "README.md:1: docs/nope.md: target file does not exist" in capsys.readouterr().err
    assert readme.read_bytes() == before


def test_repository_markdown_links_and_anchors_resolve_without_network() -> None:
    assert check_files(ROOT, tracked_markdown(ROOT)) == []
