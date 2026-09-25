"""Persistent unattended watcher for the StageMesh engineering controller.

Engineering infrastructure only (see `tooling/build_coordinator/README.md`
and `docs/architecture/sdd_001_persistent_watcher_architecture.md`). The
watcher never replaces `BuildRunner.run_once()`; it wraps it with operator
lifecycle commands, unattended Windows startup, crash restart, repository
authorization, GitHub label provisioning, transient-failure backoff, and
safe operational logging.
"""
