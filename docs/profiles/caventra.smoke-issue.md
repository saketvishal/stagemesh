# StageMesh smoke: prove the profile works on this repo

Title: `StageMesh smoke: docs-only readiness note`
Labels: `stagemesh:smoke`, `stagemesh:prep`, `area:docs`, `priority:p3`

Body:

> Objective: add one line to `docs/engineering/stagemesh-smoke.md` recording the date of the last StageMesh smoke run.
> Docs only. Do not touch application code, migrations, lockfiles, CI or secrets.

Why it is safe: the labels select the `prep` type (light validation, `docs/**` and `README.md` only, four static gates), so a run
exercises claim, worktree, validation, review and integration without touching product code.

Check it without implementing anything:

```
stagemesh --project C:\caventra project-smoke --dry-run-selection
stagemesh --project C:\caventra project-smoke --task <issue number>
stagemesh --project C:\caventra continue --task <issue number> --dry-run   # only when you want the full lifecycle
```
