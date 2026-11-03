"""Public, provider-neutral authorization checks for bounded Codex MCP configuration."""

from stagemesh import provider_authorization as pa


def test_unattended_codex_accepts_only_runtime_validated_mcp_keys() -> None:
    key = "mcp_servers.fixture.command"
    argv = ["codex", "exec", "--ignore-user-config", "-c", f'{key}="fixture-server"']

    assert pa.denial("codex", argv, {"codex"}, unattended=True)
    assert (
        pa.denial(
            "codex",
            argv,
            {"codex"},
            unattended=True,
            permitted_codex_config=frozenset({key}),
        )
        is None
    )
    assert pa.denial(
        "codex",
        [*argv, "-c", 'mcp_servers.unapproved.command="fixture-executable"'],
        {"codex"},
        unattended=True,
        permitted_codex_config=frozenset({key}),
    )
