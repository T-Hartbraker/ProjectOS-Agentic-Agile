"""Snapshot rendering for collected report DTOs. No database access or writes."""

from __future__ import annotations

import html
import re
from typing import Any

from projectos.errors import OrchestrationError
from projectos.store import require_safe_id

DOWNLOAD_FORMATS = {
    "html": "text/html; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "pdf": "application/pdf",
}
_FORMAT_ALIASES = {"md": "markdown"}
_SNAPSHOT_NOTICE = (
    "This file is a generated snapshot of ProjectOS state. "
    "It is not the system of record and does not replace stored jobs, "
    "QA evidence, or release records."
)
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_download_format(value: str) -> str:
    text = str(value or "").strip().lower()
    text = _FORMAT_ALIASES.get(text, text)
    if (
        text not in DOWNLOAD_FORMATS
        or "/" in str(value)
        or "\\" in str(value)
        or ".." in str(value)
    ):
        raise OrchestrationError(f"report format {value!r} not found")
    return text


def report_download_filename(report: dict[str, Any], fmt: str) -> str:
    fmt = require_download_format(fmt)
    project = require_safe_id(
        str(report.get("project_human_id") or "project"), label="project"
    )
    kind = require_safe_id(str(report.get("report_kind") or "report"), label="kind")
    revision = str(report.get("revision") or "snapshot")
    if not _SAFE_TOKEN_RE.fullmatch(revision):
        revision = "snapshot"
    ext = {"html": "html", "markdown": "md", "pdf": "pdf"}[fmt]
    name = f"{project}_{kind}_{revision[:16]}.{ext}"
    if "/" in name or "\\" in name or ".." in name:
        raise OrchestrationError("report filename is not a valid identifier")
    return name


def render_report_download(report: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Render a collected envelope to downloadable bytes. Does not persist."""
    chosen = require_download_format(fmt)
    if chosen == "html":
        payload = render_report_html(report).encode("utf-8")
    elif chosen == "markdown":
        payload = render_report_markdown(report).encode("utf-8")
    else:
        payload = render_report_pdf(report)
    return {
        "format": chosen,
        "media_type": DOWNLOAD_FORMATS[chosen],
        "filename": report_download_filename(report, chosen),
        "content": payload,
        "revision": report.get("revision"),
        "generated_at": report.get("generated_at"),
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    """Render a collected report envelope. Does not load orchestration state."""
    title = str(report.get("title") or report.get("report_kind") or "Report")
    lines = [
        f"# {title}",
        "",
        _SNAPSHOT_NOTICE,
        "",
        f"- kind: {report.get('report_kind')}",
        f"- project: {report.get('project_human_id')}",
        f"- iteration: {report.get('iteration_human_id') or 'Not reported'}",
        f"- release: {report.get('release_human_id') or 'Not reported'}",
        f"- generated_at: {report.get('generated_at') or 'Not reported'}",
        f"- revision: {report.get('revision') or 'Not reported'}",
        "",
        "## Sources",
    ]
    sources = report.get("sources") or []
    if not sources:
        lines.append("- No sources cited")
    for source in sources:
        timestamp = source.get("timestamp") or "Not reported"
        lines.append(
            f"- {source.get('entity_type')} {source.get('entity_human_id')} @ {timestamp}"
        )
    lines.extend(["", "## Body"])
    lines.extend(_markdown_value(report.get("body") or {}, depth=0))
    return "\n".join(lines) + "\n"


def render_report_html(report: dict[str, Any]) -> str:
    title = html.escape(str(report.get("title") or report.get("report_kind") or "Report"))
    rows = [
        ("Kind", report.get("report_kind")),
        ("Project", report.get("project_human_id")),
        ("Iteration", report.get("iteration_human_id")),
        ("Release", report.get("release_human_id")),
        ("Generated at", report.get("generated_at")),
        ("Revision", report.get("revision")),
    ]
    meta = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(_scalar(value))}</td></tr>"
        for label, value in rows
    )
    sources = report.get("sources") or []
    if sources:
        source_items = "".join(
            "<li>"
            f"{html.escape(str(src.get('entity_type')))} "
            f"{html.escape(str(src.get('entity_human_id')))} @ "
            f"{html.escape(_scalar(src.get('timestamp')))}"
            "</li>"
            for src in sources
        )
    else:
        source_items = "<li>No sources cited</li>"
    body_html = _html_value(report.get("body") or {})
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 2rem; color: #111; }}
    .notice {{ border: 1px solid #888; padding: 0.8rem; margin-bottom: 1.2rem; }}
    table.meta {{ border-collapse: collapse; margin-bottom: 1.2rem; }}
    th, td {{ text-align: left; padding: 0.25rem 0.8rem 0.25rem 0; vertical-align: top; }}
    @media print {{ a {{ color: inherit; text-decoration: none; }} }}
  </style>
</head>
<body>
  <p class="notice">{html.escape(_SNAPSHOT_NOTICE)}</p>
  <h1>{title}</h1>
  <table class="meta">{meta}</table>
  <h2>Sources</h2>
  <ul>{source_items}</ul>
  <h2>Body</h2>
  {body_html}
</body>
</html>
"""


def render_report_pdf(report: dict[str, Any]) -> bytes:
    """Minimal PDF snapshot. No external renderer and no filesystem writes."""
    wrapped: list[str] = []
    for line in render_report_markdown(report).splitlines() or [""]:
        text = line.replace("\t", "    ")
        if not text:
            wrapped.append("")
            continue
        while len(text) > 96:
            wrapped.append(text[:96])
            text = text[96:]
        wrapped.append(text)
    pages = [wrapped[i : i + 58] for i in range(0, max(len(wrapped), 1), 58)]
    objects: list[bytes] = [
        b"%PDF-1.4\n",
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
    ]
    page_count = len(pages)
    page_ids = list(range(4, 4 + page_count))
    content_ids = list(range(4 + page_count, 4 + 2 * page_count))
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(
        f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {page_count} >> endobj\n".encode(
            "latin-1"
        )
    )
    objects.append(
        b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
    )
    for page_id, content_id, page_lines in zip(page_ids, content_ids, pages):
        commands = ["BT", "/F1 10 Tf", "50 742 Td"]
        for index, line in enumerate(page_lines):
            safe = _pdf_escape(_latin1(line))
            if index:
                commands.append("0 -12 Td")
            commands.append(f"({safe}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", "replace")
        objects.append(
            (
                f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >> endobj\n"
            ).encode("latin-1")
        )
        objects.append(
            f"{content_id} 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1")
            + stream
            + b"\nendstream\nendobj\n"
        )
    content = b"".join(objects)
    offsets = []
    cursor = 0
    for chunk in objects:
        offsets.append(cursor)
        cursor += len(chunk)
    xref = [b"xref\n", f"0 {len(objects)}\n".encode("latin-1"), b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("latin-1"))
    trailer = (
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{len(content)}\n%%EOF\n"
    ).encode("latin-1")
    return content + b"".join(xref) + trailer


def _markdown_value(value: Any, *, depth: int) -> list[str]:
    indent = "  " * depth
    if isinstance(value, dict):
        if not value:
            return [f"{indent}- {{}}"]
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}- {key}:")
                lines.extend(_markdown_value(item, depth=depth + 1))
            else:
                lines.append(f"{indent}- {key}: {_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{indent}- []"]
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}-")
                lines.extend(_markdown_value(item, depth=depth + 1))
            else:
                lines.append(f"{indent}- {_scalar(item)}")
        return lines
    return [f"{indent}- {_scalar(value)}"]


def _html_value(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return "<p>Not reported</p>"
        items = [
            f"<li><strong>{html.escape(str(key))}</strong>: {_html_value(item)}</li>"
            for key, item in value.items()
        ]
        return f"<ul>{''.join(items)}</ul>"
    if isinstance(value, list):
        if not value:
            return "<p>None reported</p>"
        return "<ul>" + "".join(f"<li>{_html_value(item)}</li>" for item in value) + "</ul>"
    return html.escape(_scalar(value))


def _scalar(value: Any) -> str:
    if value is None or value == "":
        return "Not reported"
    return str(value)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")
