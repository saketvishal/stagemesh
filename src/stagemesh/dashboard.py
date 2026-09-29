from __future__ import annotations

import html

from .operator import operator_report
from .persistence import Store


def render_dashboard(store: Store) -> str:
    report = operator_report(store)
    summary_cards = render_summary(report.lines)
    sections = "\n".join(render_section(section.name, section.rows) for section in report.sections)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>StageMesh Dashboard</title>"
        "<style>body{font-family:system-ui;margin:2rem;max-width:1120px;color:#1f2328}"
        "h1{font-size:28px} h2{font-size:18px;margin-top:2rem}"
        ".summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:1rem 0 1.5rem}"
        ".metric{border:1px solid #d0d7de;border-radius:6px;padding:10px;background:#f6f8fa}"
        ".metric strong{display:block;font-size:13px;color:#57606a}.metric span{font-size:20px;font-weight:700}"
        "table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}"
        "th,td{border:1px solid #d0d7de;padding:6px 8px;text-align:left;font-size:14px}"
        "th{background:#f6f8fa}.ok{color:#116329;font-weight:600}.empty{color:#57606a}</style></head>"
        f"<body><h1>StageMesh Dashboard</h1><p class=\"ok\">summary: {html.escape(report.summary)}</p>"
        f"{summary_cards}{sections}</body></html>"
    )


def dashboard_summary(store: Store) -> dict[str, str]:
    report = operator_report(store)
    return dict(summary_pairs(report.lines))


def render_summary(lines: tuple[str, ...]) -> str:
    cards = "".join(
        f"<div class=\"metric\"><strong>{html.escape(label)}</strong><span>{html.escape(value)}</span></div>"
        for label, value in summary_pairs(lines)
    )
    return f"<section><h2>Status Summary</h2><div class=\"summary\">{cards}</div></section>"


def summary_pairs(lines: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "tasks",
            "running",
            "done",
            "backlog",
            "blocked_tasks",
            "failed_executions",
            "unknown_executions",
            "workers",
            "recent_source_events",
            "retry_states",
            "external_evidence",
        }:
            pairs.append((key.replace("_", " "), value))
    return tuple(pairs)


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
