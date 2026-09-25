import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rewrite_history_remove_ai_attribution import strip_ai_attribution


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
