import sys
from subprocess import CompletedProcess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rewrite_history_remove_ai_attribution import build_parser, cmd_push_plan, cmd_verify, strip_ai_attribution


def test_removes_claude_co_authored_by_trailer():
    message = (
        "feat: add widget\n\n"
        "Some body text.\n\n"
        "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n"
    )
    assert strip_ai_attribution(message) == "feat: add widget\n\nSome body text.\n"


def test_removes_multiple_provider_trailers_across_a_squash_message():
    message = (
        "feat: bootstrap repair (#63)\n\n"
        "* feat: project-owned backlogs\n\n"
        "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n\n"
        "* feat: add project definition\n\n"
        "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n"
    )
    result = strip_ai_attribution(message)
    assert "anthropic" not in result.lower()
    assert "co-authored-by" not in result.lower()
    assert "* feat: project-owned backlogs" in result
    assert "* feat: add project definition" in result


def test_removes_generated_with_link():
    message = "fix: bug\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    result = strip_ai_attribution(message)
    assert "generated with" not in result.lower()
    assert result.strip() == "fix: bug"


def test_preserves_human_co_authored_by_trailer():
    message = "feat: pairing session\n\nCo-Authored-By: Jane Doe <jane@example.com>\n"
    assert strip_ai_attribution(message) == message


def test_preserves_body_content_unrelated_to_trailers():
    message = "fix: handle edge case\n\nThis fixes a race in the claim recovery path.\n"
    assert strip_ai_attribution(message) == message


def test_strips_trailing_blank_lines_left_after_removal():
    message = "chore: tidy\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n\n\n"
    result = strip_ai_attribution(message)
    assert result == "chore: tidy\n"


def test_verify_writes_mapping_and_checks_topology(monkeypatch, tmp_path):
    calls = {
        ("git", "rev-list", "--reverse", "--parents", "old"): CompletedProcess([], 0, "old1\nold2 old1\n", ""),
        ("git", "rev-list", "--reverse", "--parents", "new"): CompletedProcess([], 0, "new1\nnew2 new1\n", ""),
        ("git", "rev-parse", "old1^{tree}"): CompletedProcess([], 0, "tree-a\n", ""),
        ("git", "rev-parse", "new1^{tree}"): CompletedProcess([], 0, "tree-a\n", ""),
        ("git", "rev-parse", "old2^{tree}"): CompletedProcess([], 0, "tree-b\n", ""),
        ("git", "rev-parse", "new2^{tree}"): CompletedProcess([], 0, "tree-b\n", ""),
    }

    monkeypatch.setattr("rewrite_history_remove_ai_attribution._run", lambda args, **_kwargs: calls[tuple(args)])
    mapping = tmp_path / "sha-map.json"
    args = build_parser().parse_args(["verify", "--old", "old", "--new", "new", "--mapping-out", str(mapping)])

    assert cmd_verify(args) == 0
    assert '"old": "old1"' in mapping.read_text(encoding="utf-8")
    assert '"new": "new2"' in mapping.read_text(encoding="utf-8")


def test_verify_rejects_parent_topology_mismatch(monkeypatch):
    calls = {
        ("git", "rev-list", "--reverse", "--parents", "old"): CompletedProcess([], 0, "old1\nold2 old1\n", ""),
        ("git", "rev-list", "--reverse", "--parents", "new"): CompletedProcess([], 0, "new1\nnew2 different-parent\n", ""),
        ("git", "rev-parse", "old1^{tree}"): CompletedProcess([], 0, "tree-a\n", ""),
        ("git", "rev-parse", "new1^{tree}"): CompletedProcess([], 0, "tree-a\n", ""),
        ("git", "rev-parse", "old2^{tree}"): CompletedProcess([], 0, "tree-b\n", ""),
        ("git", "rev-parse", "new2^{tree}"): CompletedProcess([], 0, "tree-b\n", ""),
    }

    monkeypatch.setattr("rewrite_history_remove_ai_attribution._run", lambda args, **_kwargs: calls[tuple(args)])
    args = build_parser().parse_args(["verify", "--old", "old", "--new", "new"])

    assert cmd_verify(args) == 1


def test_push_plan_prints_force_with_lease_equivalent(monkeypatch, capsys):
    calls = {
        ("git", "rev-parse", "backup"): CompletedProcess([], 0, "oldsha\n", ""),
        ("git", "rev-parse", "main"): CompletedProcess([], 0, "newsha\n", ""),
    }

    monkeypatch.setattr("rewrite_history_remove_ai_attribution._run", lambda args, **_kwargs: calls[tuple(args)])
    args = build_parser().parse_args(["push-plan", "--old", "backup", "--new", "main"])

    assert cmd_push_plan(args) == 0
    output = capsys.readouterr().out
    assert "git push --force-with-lease=main:oldsha origin newsha:refs/heads/main" in output
    assert "Do not delete the backup ref automatically" in output
