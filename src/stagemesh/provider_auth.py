"""Provider login-state checks that never make a model call and never expose a credential.

`claude auth status` and `codex login status` only read local login state. Their output is reduced to a small classification;
raw output is not returned. API-key environment variables are reported by NAME only, because they can silently move a CLI
from a flat subscription onto metered billing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

BILLING_ENVIRONMENT_VARIABLES = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
KNOWN_CLAUDE_METHODS = frozenset({"none", "claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party"})
REAL_PROVIDER_EXECUTABLES = frozenset({"codex", "claude", "grok", "agy"})
SUBSCRIPTION_MODES = frozenset({"claude.ai", "chatgpt_subscription"})
_STATUS_COMMANDS = {"claude": ("claude", "auth", "status"), "codex": ("codex", "login", "status")}

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class ProviderAuth:
    provider: str
    installed: bool
    auth_mode: str  # claude.ai, chatgpt_subscription, api_key, oauth_token, not_logged_in, unparsed, not_installed, ...
    subscription: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "installed": self.installed,
            "auth_mode": self.auth_mode,
            "subscription": self.subscription,
        }


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )


def classify_claude(stdout: str, exit_code: int) -> str:
    """Only an exit-0 report that says `claude.ai` is a subscription; every other outcome is named or fails closed."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return "not_logged_in" if exit_code != 0 else "unparsed"
    if not isinstance(payload, dict):
        return "unparsed"
    method = str(payload.get("authMethod") or "").strip()
    if exit_code != 0 or payload.get("loggedIn") is False:
        return "not_logged_in"
    return method if method in KNOWN_CLAUDE_METHODS else "unparsed"


_CODEX_SUBSCRIPTION = re.compile(r"^\s*logged in (?:using|with|via)\s+chatgpt\b", re.IGNORECASE | re.MULTILINE)


def classify_codex(output: str, exit_code: int) -> str:
    """Checked in this order so that doubt never becomes a subscription: API key, not logged in, then an exact ChatGPT line."""
    if re.search(r"api[ _-]?key", output, re.IGNORECASE):
        return "api_key"
    if exit_code != 0 or re.search(r"not logged|logged out|not signed", output, re.IGNORECASE):
        return "not_logged_in"
    if _CODEX_SUBSCRIPTION.search(output):
        return "chatgpt_subscription"
    return "unparsed"


def probe(provider: str, runner: Runner = _run, which: Callable[[str], str | None] = shutil.which) -> ProviderAuth:
    argv = _STATUS_COMMANDS[provider]
    executable = which(argv[0])
    if executable is None:
        return ProviderAuth(provider, False, "not_installed", False)
    try:
        result = runner([executable, *argv[1:]])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ProviderAuth(provider, True, "unparsed", False)
    if provider == "claude":
        mode = classify_claude(result.stdout or "", result.returncode)
    else:
        mode = classify_codex((result.stdout or "") + (result.stderr or ""), result.returncode)
    return ProviderAuth(provider, True, mode, mode in SUBSCRIPTION_MODES)


def billing_environment(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of API-billing variables visible to this process, and on Windows to the user's logon sessions."""
    found = [f"process:{name}" for name in BILLING_ENVIRONMENT_VARIABLES if (environ if environ is not None else os.environ).get(name)]
    if environ is None and sys.platform == "win32":
        import winreg

        for label, hive, path in (
            ("user", winreg.HKEY_CURRENT_USER, "Environment"),
            ("machine", winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    for name in BILLING_ENVIRONMENT_VARIABLES:
                        try:
                            if winreg.QueryValueEx(key, name)[0]:
                                found.append(f"{label}:{name}")
                        except FileNotFoundError:
                            continue
            except OSError:
                continue
    return found


PROVIDERS = ("claude", "codex")  # the CLIs whose login mode can be verified without a model call


def check(
    providers: Sequence[str] = PROVIDERS,
    *,
    runner: Runner = _run,
    environ: Mapping[str, str] | None = None,
    require_installed: bool = False,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    states = [probe(name, runner, which) for name in providers]
    billing = billing_environment(environ)
    installed = [state for state in states if state.installed]
    blockers: list[str] = []
    if billing:
        blockers.append("API billing variables are set (" + ", ".join(billing) + "); unset them so subscription login is used")
    blockers.extend(f"{state.provider} is logged in as {state.auth_mode}, not a subscription" for state in installed if not state.subscription)
    if require_installed:
        blockers.extend(f"{state.provider} CLI is not installed" for state in states if not state.installed)
    elif not installed:
        blockers.append("no coding CLI (claude or codex) is installed")
    return {
        "providers": [state.to_dict() for state in states],
        "api_billing_environment": billing,
        "ok": not blockers,
        "blockers": blockers,
        "model_calls_made": 0,
    }


def uses_real_providers(commands: Sequence[Sequence[str]]) -> bool:
    """True when any configured provider command launches a real coding CLI (codex, claude, grok, agy)."""
    for command in commands:
        for token in command:
            name = os.path.basename(str(token).replace("\\", "/")).lower()
            if name.rsplit(".", 1)[0] in REAL_PROVIDER_EXECUTABLES:
                return True  # any argv token: wrappers such as `cmd /c codex` or `npx claude` still launch a real CLI
    return False
