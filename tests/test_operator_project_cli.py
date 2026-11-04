from __future__ import annotations

from types import SimpleNamespace

import pytest

from stagemesh import api_server, cli, ui_launcher
from stagemesh.persistence import Store
from stagemesh.registry import GlobalRegistry, ProjectRegistration


class FakeServer:
    def __init__(self, port: int):
        self.server_address = ("127.0.0.1", port)
        self.served = False
        self.closed = False
        self.stopped = False

    def serve_forever(self) -> None:
        self.served = True

    def shutdown(self) -> None:
        self.stopped = True

    def server_close(self) -> None:
        self.closed = True


class FakeTickets:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.count = 0

    def issue(self) -> str:
        self.count += 1
        return f"{self.prefix}-{self.count}"


def _project(path) -> None:
    path.mkdir()
    store = Store(cli.db_path(path))
    store.migrate()
    store.close()


def test_ui_registry_launches_separate_servers_tokens_and_projections(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _project(alpha)
    _project(beta)
    registry_path = tmp_path / "registry.json"
    GlobalRegistry(registry_path).save(
        [
            ProjectRegistration("Alpha", alpha, cli.db_path(alpha)),
            ProjectRegistration("Beta", beta, cli.db_path(beta)),
        ]
    )
    created: list[tuple[FakeServer, SimpleNamespace, object]] = []

    def fake_server(db, token=None, port=0, static_dir=None, api_class=None, control=None, projects=None):
        index = len(created) + 1
        server = FakeServer(44000 + index)
        api = SimpleNamespace(
            token=f"token-{index}",
            launch_tickets=FakeTickets(f"ticket-{index}"),
            projects=None,
        )
        created.append((server, api, control))
        return server, api

    opened: list[str] = []
    monkeypatch.setattr(api_server, "make_server", fake_server)
    monkeypatch.setattr(
        ui_launcher,
        "launch_ui",
        lambda url: opened.append(url) or ui_launcher.UiLaunch("browser-app", "browser"),
    )

    assert cli.main(
        [
            "--project",
            str(alpha),
            "ui",
            "--registry",
            str(registry_path),
            "--enable-control",
        ]
    ) == 0

    assert len(created) == 2
    assert created[0][0].server_address != created[1][0].server_address
    assert created[0][1].token != created[1][1].token
    assert all(control is not None for _server, _api, control in created)
    assert opened == ["http://127.0.0.1:44001/#token=token-1"]
    for server, api, _control in created:
        projection = api.projects.serialize()
        assert projection["current_project_id"]
        assert len(projection["projects"]) == 2
        assert all("token-" not in str(row["launch_url"]) for row in projection["projects"])
        assert server.closed


def test_ui_registry_refuses_to_infer_unregistered_current_project(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _project(alpha)
    _project(beta)
    registry_path = tmp_path / "registry.json"
    GlobalRegistry(registry_path).save(
        [ProjectRegistration("Beta", beta, cli.db_path(beta))]
    )
    monkeypatch.setattr(
        api_server,
        "make_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("server started")),
    )

    assert cli.main(
        ["--project", str(alpha), "ui", "--registry", str(registry_path)]
    ) == 2
    assert "no default was inferred" in capsys.readouterr().err
