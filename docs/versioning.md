# Versioning and stable installs

StageMesh uses git tags as reinstallable stable package points. The current known-good stable line is:

```powershell
python -m pip install "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@v0.1.0"
```

Use that tag when a project needs the last proven operating version. For example, a product repository can refresh its project-local StageMesh runtime from the stable tag instead of from `main`.

Development on `main` moves to the next dev version after a stable tag is cut. That keeps experimental or in-progress StageMesh work from looking like the same package as the known-good release.

Current lines:

- `v0.1.0`: stable fallback tag at the post-task-266 operating baseline.
- `main`: `0.2.0.dev0`, for continued StageMesh development.

Release rule:

1. Cut an annotated tag from a clean, proven `main`.
2. Push the tag.
3. Bump `main` to the next `.dev0` version.
4. Refresh project-local runtimes from a tag when stability matters, or from `main` when testing new StageMesh features.
