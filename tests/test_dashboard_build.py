"""Dashboard build helpers for API static serving."""

from __future__ import annotations

from pathlib import Path

from projectos.dashboard_build import built_dashboard_contains, dashboard_needs_build


def test_dashboard_needs_build_when_dist_missing(tmp_path: Path, monkeypatch) -> None:
    web = tmp_path / "web"
    src = web / "src"
    src.mkdir(parents=True)
    (web / "index.html").write_text("<html></html>", encoding="utf-8")
    (web / "package.json").write_text("{}", encoding="utf-8")
    (web / "vite.config.ts").write_text("export default {}", encoding="utf-8")
    (src / "App.tsx").write_text("export {}", encoding="utf-8")
    monkeypatch.setattr("projectos.dashboard_build.PROJECTOS_ROOT", tmp_path)
    monkeypatch.setattr("projectos.dashboard_build.WEB_DIR", web)
    monkeypatch.setattr(
        "projectos.dashboard_build.dashboard_index",
        lambda: web / "dist" / "index.html",
    )
    assert dashboard_needs_build() is True


def test_built_dashboard_contains_marker(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "web" / "dist" / "assets"
    dist.mkdir(parents=True)
    (dist / "index-test.js").write_text("settings-link /settings SlackSettings", encoding="utf-8")
    monkeypatch.setattr("projectos.dashboard_build.DASHBOARD_DIST", tmp_path / "web" / "dist")
    assert built_dashboard_contains("settings-link") is True
    assert built_dashboard_contains("missing-marker") is False
