from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from stagemesh import api_server, ui_launcher
from stagemesh import cli as cli_module

TOKEN = "fixture-secret-token"


class _Server:
    server_address = ("127.0.0.1", 43123)

    def __init__(self) -> None:
        self.served = False
        self.closed = False

    def serve_forever(self) -> None:
        self.served = True

    def server_close(self) -> None:
        self.closed = True


def test_windows_prefers_a_chromeless_edge_app_without_shell_or_output_handles() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return object()

    launch = ui_launcher.launch_ui(
        f"http://127.0.0.1:43123/#token={TOKEN}",
        platform_name="win32",
        environ={"ProgramFiles": "C:/Apps"},
        which=lambda _name: None,
        exists=lambda path: path.name == "msedge.exe",
        popen=popen,
    )

    assert launch.method == "browser-app" and launch.executable is not None
    command, kwargs = calls[0]
    assert command[0].endswith("msedge.exe")
    assert command[-1] == f"--app=http://127.0.0.1:43123/#token={TOKEN}"
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }


def test_windows_falls_back_to_the_system_browser_without_exposing_the_failure() -> None:
    opened: list[str] = []

    launch = ui_launcher.launch_ui(
        f"http://127.0.0.1:43123/#token={TOKEN}",
        platform_name="win32",
        environ={"ProgramFiles": "C:/Apps"},
        which=lambda _name: None,
        exists=lambda _path: True,
        popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret-bearing command failed")),
        startfile=opened.append,
    )

    assert launch.method == "system-browser"
    assert opened == [f"http://127.0.0.1:43123/#token={TOKEN}"]


def test_launcher_failure_message_never_contains_the_url_or_token() -> None:
    with pytest.raises(ui_launcher.UiLaunchError) as raised:
        ui_launcher.launch_ui(
            f"http://127.0.0.1:43123/#token={TOKEN}",
            platform_name="linux",
            which=lambda _name: None,
        )
    assert TOKEN not in str(raised.value) and "http://" not in str(raised.value)


def test_linux_system_browser_has_no_inherited_output_and_ignores_browser_environment() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return object()

    launch = ui_launcher.launch_ui(
        f"http://127.0.0.1:43123/#token={TOKEN}",
        platform_name="linux",
        environ={"BROWSER": "token-printing-helper"},
        which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
        popen=popen,
    )

    assert launch == ui_launcher.UiLaunch("system-browser", "/usr/bin/xdg-open")
    command, kwargs = calls[0]
    assert command == ["/usr/bin/xdg-open", f"http://127.0.0.1:43123/#token={TOKEN}"]
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": {},
    }


@pytest.mark.parametrize("port", [-1, 65536])
def test_ui_command_rejects_invalid_ports_before_binding_or_printing_a_token(
    port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_server(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid port reached server creation")

    monkeypatch.setattr(api_server, "make_server", unexpected_server)

    assert cli_module.main(["--project", str(tmp_path), "ui", "--port", str(port)]) == 2

    captured = capsys.readouterr()
    assert "port must be between 0 and 65535" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_ui_command_opens_then_serves_without_printing_or_returning_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _Server()
    observed: list[str] = []
    monkeypatch.setattr(
        api_server,
        "make_server",
        lambda *_args, **_kwargs: (server, SimpleNamespace(token=TOKEN)),
    )
    monkeypatch.setattr(
        ui_launcher,
        "launch_ui",
        lambda url: observed.append(url) or ui_launcher.UiLaunch("browser-app", "edge.exe"),
    )

    assert cli_module.main(["--project", str(tmp_path), "ui", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "executable": "edge.exe",
        "launch_method": "browser-app",
        "port": 43123,
        "url": "http://127.0.0.1:43123/",
    }
    assert observed == [f"http://127.0.0.1:43123/#token={TOKEN}"]
    assert TOKEN not in captured.out + captured.err
    assert server.served and server.closed


def test_ui_command_closes_the_server_when_no_browser_can_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _Server()
    monkeypatch.setattr(
        api_server,
        "make_server",
        lambda *_args, **_kwargs: (server, SimpleNamespace(token=TOKEN)),
    )
    monkeypatch.setattr(
        ui_launcher,
        "launch_ui",
        lambda _url: (_ for _ in ()).throw(ui_launcher.UiLaunchError("no usable browser could be opened")),
    )

    assert cli_module.main(["--project", str(tmp_path), "ui"]) == 2

    captured = capsys.readouterr()
    assert "no usable browser" in captured.err and TOKEN not in captured.out + captured.err
    assert server.closed and not server.served
