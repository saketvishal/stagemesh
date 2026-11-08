from __future__ import annotations

from stagemesh.github import GitHubClient


class LabelTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]]):
        self.responses = responses
        self.requests: list[tuple[str, str, object]] = []

    def request(self, method: str, path: str, body=None):
        self.requests.append((method, path, body))
        return self.responses.pop(0)


def test_ensure_label_preserves_existing_repository_label() -> None:
    transport = LabelTransport([(200, {}, {"name": "stagemesh:ready", "color": "operator-color"})])
    client = GitHubClient("acme", "widgets", transport)

    result = client.ensure_label("stagemesh:ready", "2DA44E", "ready")

    assert result.status == "OK"
    assert transport.requests == [
        ("GET", "/repos/acme/widgets/labels/stagemesh%3Aready", None),
    ]


def test_ensure_label_creates_only_when_missing_and_issue_labeling_is_additive() -> None:
    transport = LabelTransport(
        [
            (404, {}, {}),
            (201, {}, {"name": "stagemesh:needs-triage"}),
            (200, {}, [{"name": "stagemesh:needs-triage"}]),
        ]
    )
    client = GitHubClient("acme", "widgets", transport)

    ensured = client.ensure_label("stagemesh:needs-triage", "D4C5F9", "triage")
    labeled = client.add_issue_labels("42", ("stagemesh:needs-triage",))

    assert ensured.status == labeled.status == "OK"
    assert transport.requests == [
        ("GET", "/repos/acme/widgets/labels/stagemesh%3Aneeds-triage", None),
        (
            "POST",
            "/repos/acme/widgets/labels",
            {"name": "stagemesh:needs-triage", "color": "D4C5F9", "description": "triage"},
        ),
        (
            "POST",
            "/repos/acme/widgets/issues/42/labels",
            {"labels": ["stagemesh:needs-triage"]},
        ),
    ]
