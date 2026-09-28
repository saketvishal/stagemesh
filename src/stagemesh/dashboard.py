from __future__ import annotations

import html

from .operator import operator_report
from .persistence import Store


def render_dashboard(store: Store) -> str:
    report = operator_report(store)
    rows = "\n".join(f"<li>{html.escape(line)}</li>" for line in report.lines)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>StageMesh Dashboard</title>"
        "<style>body{font-family:system-ui;margin:2rem;max-width:960px}"
        "code,li{font-size:14px} .ok{color:#116329}</style></head>"
        f"<body><h1>StageMesh Dashboard</h1><p class=\"ok\">{html.escape(report.summary)}</p>"
        f"<ul>{rows}</ul></body></html>"
    )
