from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = REPO_ROOT / "docs" / "dogfood" / "acceptance-suite.yaml"
DEMO_ROOT = REPO_ROOT / "examples" / "public_dogfood"

REQUIRED_REQUIREMENTS = {
    "parallelism",
    "review",
    "validation",
    "provider fallback",
    "controlled interruption and recovery",
    "cleanup",
    "global invocation",
    "GitHub delivery",
    "single-agent public demo",
    "staged agents public demo",
    "cross-provider recovery",
    "high-risk governance",
    "multi-project execution",
}

FORBIDDEN_TEXT = re.compile(
    r"(?i)(caventra|C:\\|[A-Za-z]:[\\/]|(?:token|secret|api[_-]?key|password)\s*[:=]\s*\S+)"
)


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


def test_public_dogfood_suite_covers_gh50_acceptance_surface():
    suite = _load_yaml(SUITE_PATH)
    scenarios = suite["scenarios"]
    covered = {requirement for scenario in scenarios for requirement in scenario["requirements"]}

    assert REQUIRED_REQUIREMENTS <= covered
    assert suite["repeatability"] == {
        "database": "sqlite",
        "providers": "fake-or-scripted",
        "network_required": False,
        "cleanup_required": True,
    }


def test_public_dogfood_scenarios_reference_existing_demo_manifests():
    suite = _load_yaml(SUITE_PATH)

    for scenario in suite["scenarios"]:
        demo = REPO_ROOT / scenario["demo"]
        assert demo.exists(), scenario
        manifest = _load_yaml(demo)
        assert manifest["demo_id"] == scenario["id"]
        assert "evidence" in manifest or "expected" in manifest


def test_public_dogfood_assets_are_public_safe():
    paths = [SUITE_PATH, REPO_ROOT / "docs" / "dogfood" / "README.md"]
    paths.extend(DEMO_ROOT.glob("*.yaml"))
    paths.append(DEMO_ROOT / "README.md")

    offenders = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in FORBIDDEN_TEXT.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")

    assert not offenders, offenders
