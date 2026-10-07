from __future__ import annotations

from typing import Any


def provider_no_progress_recommendation(task_id: str, no_progress: dict[str, Any] | None) -> str:
    reasons = [str(reason) for reason in (no_progress or {}).get("reasons", [])]
    exhausted_markers = (
        "all_implementation_providers_no_progress",
        "all_implementation_providers_exhausted",
        "all_implementation_providers_failed",
    )
    exhausted = any(marker in reason for reason in reasons for marker in exhausted_markers)
    if exhausted:
        attempted = ", ".join(reasons[-5:]) or "provider pool exhausted"
        return (
            f"Every eligible configured implementation provider was exhausted ({attempted}). "
            f"No candidate was produced for task {task_id}. No further automatic provider remains in this pass. "
            "After provider availability/capacity or the task contract changes, run `stagemesh continue`. "
            "No manual provider choice, diagnostic detour, state edit, or retry command is required for normal recovery."
        )
    return (
        "The configured implementation provider path did not produce a usable change yet (nothing committed, or the same tree again). "
        "Run `stagemesh continue`; StageMesh will keep applying the configured provider policy and fallback pool. "
        "No manual provider choice, diagnostic detour, state edit, or retry command is required for normal recovery."
    )


def apply_provider_no_progress_recovery_patch() -> None:
    from . import diagnosis as diagnosis_module
    from . import recovery as recovery_module

    if getattr(diagnosis_module, "_HANDS_OFF_PROVIDER_PATCHED", False):
        return

    original_diagnose = diagnosis_module.diagnose
    original_recommend = recovery_module._recommend

    def diagnose(*args: Any, **kwargs: Any):
        result = original_diagnose(*args, **kwargs)
        if result is not None and result.category == diagnosis_module.PROVIDER_NO_PROGRESS:
            result.recommendation = provider_no_progress_recommendation(result.task_id, result.no_progress)
        return result

    def _recommend(report: dict[str, Any], task_id: str, ref: str | None) -> tuple[str, str]:
        diagnosis = report.get("diagnosis")
        if report.get("status") == "BLOCKED" and diagnosis and diagnosis.get("category") == diagnosis_module.PROVIDER_NO_PROGRESS:
            return "stagemesh continue", diagnosis["recommendation"]
        return original_recommend(report, task_id, ref)

    diagnosis_module.diagnose = diagnose
    recovery_module.diagnose = diagnose
    recovery_module._recommend = _recommend
    diagnosis_module._HANDS_OFF_PROVIDER_PATCHED = True
