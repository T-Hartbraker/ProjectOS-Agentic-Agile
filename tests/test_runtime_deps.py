"""Runtime dependency helpers."""

from __future__ import annotations

from projectos.runtime_deps import ensure_http_deps, http_deps_missing


def test_http_deps_missing_reports_websocket(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "websocket" or (name == "websocket" and fromlist):
            raise ImportError("no websocket")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    missing = http_deps_missing()
    assert "websocket-client" in missing


def test_ensure_http_deps_noop_when_present(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("projectos.runtime_deps.http_deps_missing", lambda: [])
    monkeypatch.setattr("projectos.runtime_deps.subprocess.run", fake_run)
    ensure_http_deps()
    assert calls == []
