from __future__ import annotations

import http.client
import json
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from stagemesh.api_server import make_server
from stagemesh.operator_projects import (
    PROJECTS_CONTRACT,
    OperatorProjectError,
    ProjectLaunchTickets,
    ProjectProjection,
    ProjectTarget,
)
from stagemesh.persistence import Store
from stagemesh.registry import GlobalRegistry, ProjectRegistration, RegistryValidationError


def test_launch_ticket_is_target_local_one_use_and_expiring():
    clock = [100.0]
    tickets = ProjectLaunchTickets("target-token", now=lambda: clock[0], ttl_seconds=10)
    ticket = tickets.issue()

    assert tickets.redeem(ticket) == "/#token=target-token"
    with pytest.raises(OperatorProjectError, match="invalid"):
        tickets.redeem(ticket)

    expired = tickets.issue()
    clock[0] = 111.0
    with pytest.raises(OperatorProjectError, match="invalid"):
        tickets.redeem(expired)


@pytest.mark.parametrize(
    "current,targets",
    [
        ("missing", (ProjectTarget("A", "a", "http://127.0.0.1:1/", lambda: "x"),)),
        (
            "a",
            (
                ProjectTarget("A", "a", "http://127.0.0.1:1/", lambda: "x"),
                ProjectTarget("a", "b", "http://127.0.0.1:2/", lambda: "x"),
            ),
        ),
        ("a", (ProjectTarget("A", "a", "http://example.test:1/", lambda: "x"),)),
    ],
)
def test_projection_refuses_ambiguous_or_non_loopback_targets(current, targets):
    with pytest.raises(ValueError):
        ProjectProjection(current, targets)


def test_registry_refuses_malformed_boundary_casefold_and_database_duplicates(tmp_path: Path):
    registry_path = tmp_path / "registry.json"
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    outside = tmp_path / "outside.sqlite3"

    with pytest.raises(RegistryValidationError, match="inside"):
        GlobalRegistry(registry_path).save(
            [ProjectRegistration("A", project_a, outside)]
        )

    with pytest.raises(RegistryValidationError, match="name"):
        GlobalRegistry(registry_path).save(
            [
                ProjectRegistration("Alpha", project_a, project_a / "one.sqlite3"),
                ProjectRegistration("alpha", project_b, project_b / "two.sqlite3"),
            ]
        )

    nested = project_a / "nested"
    nested.mkdir()
    shared_db = nested / "state.sqlite3"
    registry_path.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": "Alpha", "path": str(project_a), "db_path": str(shared_db)},
                    {"name": "alpha", "path": str(project_b), "db_path": str(project_b / "state.sqlite3")},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegistryValidationError, match="name"):
        GlobalRegistry(registry_path).load()

    registry_path.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": "Alpha", "path": str(project_a), "db_path": str(shared_db)},
                    {"name": "Beta", "path": str(nested), "db_path": str(shared_db)},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegistryValidationError, match="database"):
        GlobalRegistry(registry_path).load()


def _store(path: Path, task_id: str) -> None:
    store = Store(path)
    store.migrate()
    store.upsert_task(task_id, source_id=task_id)
    store.close()


def _get_json(base: str, path: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_two_project_servers_keep_tokens_ports_and_databases_isolated(tmp_path: Path):
    db_a = tmp_path / "a" / "state.sqlite3"
    db_b = tmp_path / "b" / "state.sqlite3"
    _store(db_a, "only-a")
    _store(db_b, "only-b")
    server_a, api_a = make_server(db_a, token="token-a")
    server_b, api_b = make_server(db_b, token="token-b")
    base_a = f"http://127.0.0.1:{server_a.server_address[1]}/"
    base_b = f"http://127.0.0.1:{server_b.server_address[1]}/"
    targets = (
        ProjectTarget("Alpha", "project-a", base_a, api_a.launch_tickets.issue),
        ProjectTarget("Beta", "project-b", base_b, api_b.launch_tickets.issue),
    )
    api_a.projects = ProjectProjection("project-a", targets)
    api_b.projects = ProjectProjection("project-b", targets)
    threads = [
        threading.Thread(target=server_a.serve_forever, daemon=True),
        threading.Thread(target=server_b.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        status, projects = _get_json(base_a.rstrip("/"), "/api/v1/projects", "token-a")
        assert status == 200 and projects["contract"] == PROJECTS_CONTRACT
        assert projects["isolated"] is True and projects["aggregated"] is False
        beta = next(item for item in projects["projects"] if item["project_id"] == "project-b")
        assert beta["launch_url"].startswith(base_b + "project-launch?ticket=")
        assert "token-a" not in beta["launch_url"] and "token-b" not in beta["launch_url"]

        split = urlsplit(beta["launch_url"])
        connection = http.client.HTTPConnection(split.hostname, split.port, timeout=5)
        connection.request("GET", split.path + "?" + split.query)
        response = connection.getresponse()
        response.read()
        assert response.status == 303 and response.getheader("Location") == "/#token=token-b"
        connection.close()

        connection = http.client.HTTPConnection(split.hostname, split.port, timeout=5)
        connection.request("GET", split.path + "?" + split.query)
        replay = connection.getresponse()
        replay.read()
        assert replay.status == 403
        connection.close()

        _, activity_a = _get_json(base_a.rstrip("/"), "/api/v5/activity?limit=100", "token-a")
        _, activity_b = _get_json(base_b.rstrip("/"), "/api/v5/activity?limit=100", "token-b")
        assert {row["task_id"] for row in activity_a["entries"]} == {"only-a"}
        assert {row["task_id"] for row in activity_b["entries"]} == {"only-b"}
    finally:
        server_a.shutdown()
        server_b.shutdown()
        server_a.server_close()
        server_b.server_close()
