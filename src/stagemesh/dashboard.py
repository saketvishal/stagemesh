from __future__ import annotations

import html

from .operator import operator_report
from .persistence import Store


def render_dashboard(store: Store) -> str:
    report = operator_report(store)
    sections = "\n".join(render_section(section.name, section.rows) for section in report.sections)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>StageMesh Dashboard</title>"
        "<style>body{font-family:system-ui;margin:2rem;max-width:1120px;color:#1f2328}"
        "h1{font-size:28px} h2{font-size:18px;margin-top:2rem}"
        "table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}"
        "th,td{border:1px solid #d0d7de;padding:6px 8px;text-align:left;font-size:14px}"
        "th{background:#f6f8fa}.ok{color:#116329;font-weight:600}.empty{color:#57606a}</style></head>"
        f"<body><h1>StageMesh Dashboard</h1><p class=\"ok\">summary: {html.escape(report.summary)}</p>"
        f"{sections}</body></html>"
    )


def render_section(name: str, rows: tuple[dict[str, object], ...] | tuple[object, ...]) -> str:
    if not rows:
        return f"<section><h2>{html.escape(name)}</h2><p class=\"empty\">No rows</p></section>"
    first = rows[0]
    if not isinstance(first, dict):
        return f"<section><h2>{html.escape(name)}</h2><p class=\"empty\">Unsupported rows</p></section>"
    columns = list(first.keys())
    header = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row.get(column, '')))}</td>" for column in columns) + "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return f"<section><h2>{html.escape(name)}</h2><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></section>"
