# StageMesh Known-Good Baseline: STAGEMESH-REL-001

## 1. Baseline Identity

- **Baseline Tag**: `v0.5.1-rel-001`
- **Target Commit**: `8049dbdf3e8d025efeaf88d61f61791f088f5367`
- **Branch**: `fix/issue-477-admission-diagnosis`
- **Associated Pull Request**: StageMesh PR #478 (resolving Issue #477)
- **Target Project**: Caventra (`C:\development\caventra\caventra-main`)
- **Qualification Timestamp**: 2026-11-02 10:00:00 UTC-06:00
- **Qualification Verdict**: **PASS**

---

## 2. Verified Invariants and Reliability Fixes

### A. Provider Resilience & Windows CLI Buffer Protection
- **Defect**: On Windows systems, calling external CLI providers like `agy` with prompts larger than 8,191 characters via command-line arguments (`--print <prompt>`) triggered Python `subprocess.Popen` failure `[WinError 206] The filename or extension is too long`. This was misclassified as `provider_unavailable`, putting healthy providers into 900s cooldowns.
- **Fix**:
  - `src/stagemesh/providers.py`: Large prompts (>= 4000 characters) or commands lacking `--print` pass prompt text directly through standard input (`stdin`).
  - `src/stagemesh/execution.py`: Specifically classifies `WinError 206` as `command_line_too_long` instead of `provider_unavailable`.

### B. Actionable Diagnosis on No Implementation Change
- **Defect**: When an implementation provider produced zero code changes (e.g. because validation findings required modifying files outside the contract's allowed scope or tool permissions were denied), StageMesh reported an unhelpful generic `no_implementation_change` without context.
- **Fix**:
  - `src/stagemesh/providers.py`: Added `_diagnose_no_change()` to inspect worktree modifications outside the target path, tool permission rejections, and contract forbidden-file clashes, returning actionable failure reasons (e.g., `specification_insufficient: validation findings require editing files outside permitted contract scope`).

### C. GitHub API Network Resilience & Rate Limit Honoring
- **Defect**: Transient GitHub transport errors (such as `http.client.RemoteDisconnected: Remote end closed connection without response`) or 5xx server errors immediately aborted the entire execution window (e.g., stopping after 3.7 minutes into a 120-minute window).
- **Fix**:
  - `src/stagemesh/task_sources.py`: Added `TRANSIENT_NETWORK_ERRORS` retry loop with exponential backoff and jitter (`max_retries=3`).
  - Added header-aware rate limit inspection (`Retry-After`, `x-ratelimit-reset`, `x-ratelimit-remaining`).
  - `src/stagemesh/selection_scope.py`: Introduced `TransientScopeError` on unknown network status.
  - `src/stagemesh/run_window.py`: Window execution loop catches `TransientScopeError`, records audit event `selection.transient_sync_pause`, sleeps for a bounded interval, and retries rather than aborting.

### D. Governance Manifest Auto-Synchronization
- **Defect**: Structural prerequisite manifests like `.stagemesh/contracts/*.spec_manifest.json` could fail integrity validation if their recorded `candidate_revision` drifted, while simultaneously forbidding the AI provider from updating `.stagemesh/**`.
- **Fix**:
  - `src/stagemesh/validation.py`: Automatically synchronizes `candidate_revision` and `changed_files` on structural-only prerequisite manifests when candidate changes match `allowed_files`.

---

## 3. Qualification Evidence

### A. Permanent Regression Test Suite (16 passed)
Command executed in `C:\development\stagemesh\main`:
```bash
pytest tests/test_admission_diagnosis_regression.py tests/test_private_repo_403_regression.py tests/test_reliability_recovery_regression.py
```
Results:
- `tests/test_admission_diagnosis_regression.py`: 5 passed
- `tests/test_private_repo_403_regression.py`: 5 passed
- `tests/test_reliability_recovery_regression.py`: 6 passed
- Total: **16 passed in 3.49s**

Attribution Policy:
```bash
python scripts/check_attribution.py --base <base_sha> --head <head_sha>
# Attribution policy passed (0 AI attribution trailers)
```

### B. Live Acceptance Run (Window `2ec91e7f`)
- Command: `stagemesh run-window --label lane:court-intelligence --duration 15m --max-tasks 1`
- Target: Caventra `task-1967` (#424)
- Provider `agy` launched without WinError 206 crash and executed for 10m52s.
- Clean stop with zero leaked processes, zero active claims, and actionable diagnostic reporting:
  `task.implementation_unsuccessful: specification_insufficient: validation findings require editing files outside permitted contract scope`
- Report receipt: `C:\development\caventra\caventra-main\.stagemesh\reports\run-window\20261102-095038.json`

---

## 4. Rollback and Recovery Procedure

If regression or instability is detected in subsequent development, follow this procedure to restore the known-good baseline:

### Step 1: Check out the Verified Tag
```powershell
cd C:\development\stagemesh\main
git fetch origin --tags
git checkout v0.5.1-rel-001
```

### Step 2: Reinstall into the StageMesh Runtime Environment
```powershell
& "C:\Users\saket\AppData\Local\StageMesh\runtime\venv\Scripts\pip" install -e "C:\development\stagemesh\main"
```

### Step 3: Verify Runtime State
```powershell
stagemesh --version
# Expected output:
# stagemesh runtime: development (editable/source checkout)
#   package path: C:\development\stagemesh\main\src\stagemesh
#   environment: C:\Users\saket\AppData\Local\StageMesh\runtime\venv
```

### Step 4: Run the Regression Suite
```powershell
pytest tests/test_admission_diagnosis_regression.py tests/test_private_repo_403_regression.py tests/test_reliability_recovery_regression.py
```
All 16 tests must pass.
