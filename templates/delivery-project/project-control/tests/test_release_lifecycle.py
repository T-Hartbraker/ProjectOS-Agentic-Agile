"""Governed release lifecycle tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectctl import store
from projectctl.cli import main
from projectctl.db import connect
from projectctl.gitutil import fake_git_snapshot
from projectctl.release_lifecycle import ALLOWED_TRANSITIONS, RELEASE_STATUSES


SHA = "a" * 40
SHA2 = "b" * 40


def _iso_repo(tmp_path: Path, project_human_id: str = "PRJ-001") -> Path:
    root = tmp_path / "iso-repo"
    (root / "project").mkdir(parents=True, exist_ok=True)
    (root / "project" / "repository.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_type": "delivery-project",
                "project_human_id": project_human_id,
                "isolation_model": "one-project-per-repository",
            }
        ),
        encoding="utf-8",
    )
    return root


def _project(db: Path) -> None:
    store.create_project("Lifecycle", db_path=db)


def _release(db: Path, *, name: str = "MVP", version: str = "0.1.0") -> dict:
    return store.create_release(name, version=version, db_path=db)


def _qa_file(tmp_path: Path) -> Path:
    path = tmp_path / "qa-recommendation.md"
    path.write_text("# QA\n\nPASS WITH FINDINGS\n", encoding="utf-8")
    return path


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "releases" / "REL-001"
    pkg.mkdir(parents=True)
    (pkg / "release-notes.md").write_text("# Notes\n", encoding="utf-8")
    (pkg / "checksums.txt").write_text("deadbeef  release-notes.md\n", encoding="utf-8")
    return pkg


def _clean_git(sha: str = SHA):
    return fake_git_snapshot(head_sha=sha, working_tree_clean=True, known_shas={sha})


def test_happy_path_planned_candidate_qa_released(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    rel = _release(db)
    assert rel["status"] == "planned"
    assert rel.get("released_at") in (None, "")

    qa = _qa_file(tmp_path)
    pkg = _package(tmp_path)
    snap = _clean_git()

    c = store.transition_release_status(
        "REL-001",
        "candidate",
        git_sha=SHA,
        artifact_ref=str(pkg),
        git_snapshot=snap,
        db_path=db,
    )
    assert c["status"] == "candidate"
    assert c["git_sha"] == SHA
    assert c["released_at"] is None

    q = store.transition_release_status(
        "REL-001",
        "qa_passed",
        qa_evidence_ref=str(qa),
        git_snapshot=snap,
        db_path=db,
    )
    assert q["status"] == "qa_passed"
    assert q["released_at"] is None
    assert q["qa_evidence_ref"] == str(qa)

    done = store.complete_release("REL-001", db_path=db)
    assert done["status"] == "released"
    assert done["released_at"]
    assert done["git_sha"] == SHA


def test_invalid_transitions_rejected(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)

    with pytest.raises(store.StoreError, match="Invalid transition"):
        store.transition_release_status(
            "REL-001",
            "qa_passed",
            qa_evidence_ref=str(_qa_file(tmp_path)),
            git_snapshot=_clean_git(),
            db_path=db,
        )


def test_cannot_jump_planned_to_released(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    with pytest.raises(store.StoreError, match="qa_passed|Invalid transition|rejected"):
        store.complete_release("REL-001", db_path=db)

    with pytest.raises(store.StoreError, match="Invalid transition"):
        store.transition_release_status(
            "REL-001",
            "released",
            git_snapshot=_clean_git(),
            db_path=db,
        )


def test_each_transition_writes_audit(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    qa = _qa_file(tmp_path)
    pkg = _package(tmp_path)
    snap = _clean_git()

    store.transition_release_status(
        "REL-001", "candidate", git_sha=SHA, artifact_ref=str(pkg), git_snapshot=snap, db_path=db
    )
    store.transition_release_status(
        "REL-001", "qa_passed", qa_evidence_ref=str(qa), git_snapshot=snap, db_path=db
    )
    store.complete_release("REL-001", db_path=db)

    audits = store.list_audit(db_path=db, limit=20)
    actions = [a["action"] for a in audits]
    assert any("planned->candidate" in (a or "") for a in actions)
    assert any("candidate->qa_passed" in (a or "") for a in actions)
    assert any("qa_passed->released" in (a or "") for a in actions)


def test_released_at_only_on_release(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    qa = _qa_file(tmp_path)
    pkg = _package(tmp_path)
    snap = _clean_git()

    for status, kwargs in (
        (
            "candidate",
            {"git_sha": SHA, "artifact_ref": str(pkg)},
        ),
        ("qa_passed", {"qa_evidence_ref": str(qa)}),
    ):
        row = store.transition_release_status(
            "REL-001", status, git_snapshot=snap, db_path=db, **kwargs
        )
        assert row["released_at"] is None

    row = store.complete_release("REL-001", db_path=db)
    assert row["released_at"] is not None


def test_git_sha_persisted(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    store.transition_release_status(
        "REL-001",
        "candidate",
        git_sha=SHA,
        artifact_ref=str(_package(tmp_path)),
        git_snapshot=_clean_git(),
        db_path=db,
    )
    shown = store.show_release("REL-001", db_path=db)
    assert shown["git_sha"] == SHA


def test_dirty_working_tree_blocks_candidate(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    dirty = fake_git_snapshot(head_sha=SHA, working_tree_clean=False, known_shas={SHA})
    with pytest.raises(store.StoreError, match="dirty|blocked"):
        store.transition_release_status(
            "REL-001",
            "candidate",
            git_sha=SHA,
            git_snapshot=dirty,
            db_path=db,
        )


def test_dirty_tree_allowed_with_exception_decision(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    store.create_decision(
        "Allow dirty historical candidate",
        "Approve dirty-tree exception for test",
        db_path=db,
    )
    dirty = fake_git_snapshot(head_sha=SHA2, working_tree_clean=False, known_shas={SHA, SHA2})
    row = store.transition_release_status(
        "REL-001",
        "candidate",
        git_sha=SHA,
        dirty_tree_exception="DEC-001",
        artifact_ref=str(_package(tmp_path)),
        git_snapshot=dirty,
        db_path=db,
    )
    assert row["status"] == "candidate"
    assert row["dirty_tree_exception"] == "DEC-001"
    assert row["git_sha"] == SHA


def test_list_show_reflect_status(tmp_path: Path, capsys) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    repo = _iso_repo(tmp_path)
    store.create_iteration("Iter 1", goal="g", db_path=db)
    store.create_release("MVP", version="0.1.0", iteration_id="ITER-001", db_path=db)
    store.transition_release_status(
        "REL-001",
        "candidate",
        git_sha=SHA,
        artifact_ref=str(_package(tmp_path)),
        git_snapshot=_clean_git(),
        db_path=db,
    )

    rows = store.list_releases(db_path=db)
    assert rows[0]["status"] == "candidate"
    assert rows[0]["git_sha"] == SHA
    assert rows[0]["iteration"] == "ITER-001"

    base = ["--db", str(db), "--repo-root", str(repo)]
    assert main([*base, "release", "list"]) == 0
    assert main([*base, "release", "show", "REL-001"]) == 0
    out = capsys.readouterr().out
    assert "candidate" in out
    assert SHA in out


def test_cli_status_and_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    repo = _iso_repo(tmp_path)
    _release(db)
    qa = _qa_file(tmp_path)
    pkg = _package(tmp_path)

    # Patch inspect_git used when no snapshot injected via CLI path
    import projectctl.release_lifecycle as rl

    monkeypatch.setattr(
        rl,
        "inspect_git",
        lambda repo_root=None: _clean_git(),
    )

    base = ["--db", str(db), "--repo-root", str(repo)]
    assert (
        main(
            [
                *base,
                "release",
                "status",
                "REL-001",
                "candidate",
                "--git-sha",
                SHA,
                "--artifact-ref",
                str(pkg),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                *base,
                "release",
                "status",
                "REL-001",
                "qa_passed",
                "--qa-evidence",
                str(qa),
            ]
        )
        == 0
    )
    # Direct status -> released must be rejected by CLI
    assert main([*base, "release", "status", "REL-001", "released"]) == 1
    assert main([*base, "release", "complete", "REL-001"]) == 0
    shown = store.show_release("REL-001", db_path=db)
    assert shown["status"] == "released"
    assert shown["released_at"]


def test_blocking_defect_blocks_release(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store.init_database(db)
    _project(db)
    _release(db)
    qa = _qa_file(tmp_path)
    pkg = _package(tmp_path)
    snap = _clean_git()
    store.transition_release_status(
        "REL-001", "candidate", git_sha=SHA, artifact_ref=str(pkg), git_snapshot=snap, db_path=db
    )
    store.transition_release_status(
        "REL-001", "qa_passed", qa_evidence_ref=str(qa), git_snapshot=snap, db_path=db
    )
    store.create_defect("Breaks core", severity="critical", db_path=db)
    with pytest.raises(store.StoreError, match="blocking defect"):
        store.complete_release("REL-001", db_path=db)


def test_allowed_transition_table_covers_normal_path() -> None:
    assert "candidate" in ALLOWED_TRANSITIONS["planned"]
    assert "qa_passed" in ALLOWED_TRANSITIONS["candidate"]
    assert "released" in ALLOWED_TRANSITIONS["qa_passed"]
    assert "released" not in ALLOWED_TRANSITIONS["planned"]
    assert RELEASE_STATUSES >= {
        "planned",
        "candidate",
        "qa_passed",
        "released",
        "superseded",
        "withdrawn",
    }


def test_migration_adds_release_columns(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    store.init_database(db)
    conn = connect(db)
    try:
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(releases)").fetchall()
        }
        assert {
            "git_sha",
            "iteration_id",
            "artifact_ref",
            "qa_evidence_ref",
            "dirty_tree_exception",
        } <= cols
        versions = {
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert "003_release_lifecycle.sql" in versions
    finally:
        conn.close()
