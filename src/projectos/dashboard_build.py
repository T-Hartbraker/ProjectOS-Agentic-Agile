"""Build the dashboard SPA into web/dist for API static serving."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from projectos.paths import DASHBOARD_DIST, PROJECTOS_ROOT, dashboard_index, dashboard_is_built

WEB_DIR = PROJECTOS_ROOT / "web"
_BUILD_INPUTS = (
    WEB_DIR / "index.html",
    WEB_DIR / "package.json",
    WEB_DIR / "vite.config.ts",
    WEB_DIR / "tsconfig.json",
    WEB_DIR / "tsconfig.app.json",
)


def _dashboard_source_paths() -> list[Path]:
    paths = list(_BUILD_INPUTS)
    src = WEB_DIR / "src"
    if src.is_dir():
        paths.extend(path for path in src.rglob("*") if path.is_file())
    return paths


def dashboard_needs_build() -> bool:
    index = dashboard_index()
    if not index.is_file():
        return True
    dist_mtime = index.stat().st_mtime
    for path in _dashboard_source_paths():
        if path.is_file() and path.stat().st_mtime > dist_mtime:
            return True
    return False


def ensure_dashboard_built(*, force: bool = False) -> bool:
    """Build web/dist when missing or frontend sources are newer than the bundle."""
    if not (WEB_DIR / "package.json").is_file():
        return dashboard_is_built()
    if not force and not dashboard_needs_build():
        return dashboard_is_built()
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    result = subprocess.run(
        [npm, "run", "build"],
        cwd=WEB_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "dashboard build failed").strip()
        raise RuntimeError(detail)
    if not dashboard_is_built():
        raise RuntimeError(f"dashboard build finished but {dashboard_index()} is missing")
    return True


def built_dashboard_contains(marker: str) -> bool:
    """Return True when the served JS bundle contains a marker string."""
    assets = DASHBOARD_DIST / "assets"
    if not assets.is_dir():
        return False
    for path in assets.glob("index-*.js"):
        try:
            if marker in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False
