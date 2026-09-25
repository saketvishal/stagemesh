"""Persistent unattended watcher for the StageMesh engineering controller.

Engineering infrastructure only. The
watcher never replaces `BuildRunner.run_once()`; it wraps it with operator
lifecycle commands, unattended Windows startup, crash restart, repository
authorization, GitHub label provisioning, transient-failure backoff, and
safe operational logging.
"""
