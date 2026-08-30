"""Release-readiness gate: clean candidate, QA package, blocked retry."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from projectos.cli import main
from projectos.db import connection
from projectos.projectctl_bridge import ProjectctlResult
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.release_readiness import (
    GATE_READY_OUTCOME,
    assemble_qa_package,
    evaluate_release_job,
    evidence_dir_for_job,
    resolve_authoritative_projectctl_db,
    ReleaseEvaluation,
)
from projectos.release_retry import AUTHORITATIVE_INTEGRATION_SHA, reconcile_stale_release
from projectos.store import (
    add_job_dependency,
    create_job,
    get_job_by_human_id,
    mark_succeeded,
    set_job_source_provenance,
)
from projectos.worker import run_once
from projectos.worktree import current_head_sha, is_dirty

from orch_helpers import init_git_repo, seed_db, write_registry

INTEG_SHA = AUTHORITATIVE_INTEGRATION_SHA


def _cfg(tmp_path: Path, repo: Path) -> Path:
    return write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )


def _insert_qa(conn, delivery, *, result: str = "pass") -> None:
    cand = delivery.candidate_git_sha or "deadbeef"
    for role in REQUIRED_ASSURANCE:
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, delivery_job_id,
                assurance_job_id, candidate_git_sha, assurance_role, result
            ) VALUES (?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                delivery.project_human_id,
                delivery.repository_root,
                delivery.id,
                cand,
                role,
                result,
            ),
        )
    mgr = create_job(
        conn,
        human_id=f"{delivery.human_id}__QA_MANAGER",
        project_human_id=delivery.project_human_id,
        repository_root=delivery.repository_root,
        agent_role="ASSURANCE_QUALITY",
        queue="ASSURANCE_QUALITY",
        status="READY",
        iteration_human_id=delivery.iteration_human_id,
    )
    mark_succeeded(conn, mgr.id, output_ref=None, candidate_git_sha=cand)


def _seed_lineage(
    db: Path,
    repo: Path,
    *,
    integration_sha: str = INTEG_SHA,
    qa_result: str = "pass",
) -> None:
    with connection(db) as conn:
        pm = create_job(
            conn,
            human_id="JOB-P2-PM-SETUP",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="PM",
            queue="PM",
            status="READY",
            iteration_human_id="ITER-002",
        )
        mark_succeeded(conn, pm.id, output_ref=None, candidate_git_sha=None)
        arch = create_job(
            conn,
            human_id="JOB-P2-ARCH",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="ARCHITECTURE",
            queue="ARCHITECTURE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        mark_succeeded(conn, arch.id, output_ref=None, candidate_git_sha=None)
        d7 = create_job(
            conn,
            human_id="JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-002",
            work_item_type="story",
            work_item_human_id="US-007",
        )
        d7 = mark_succeeded(
            conn, d7.id, output_ref=None, candidate_git_sha="sha-us7"
        )
        _insert_qa(conn, d7, result=qa_result)
        d8 = create_job(
            conn,
            human_id="JOB-P2-DEL-PRIORITY-FILTER__REWORK-1",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-002",
            work_item_type="story",
            work_item_human_id="US-008",
        )
        d8 = mark_succeeded(
            conn, d8.id, output_ref=None, candidate_git_sha="sha-us8"
        )
        _insert_qa(conn, d8, result=qa_result)
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, integ.id, d7.id)
        add_job_dependency(conn, integ.id, d8.id)
        mark_succeeded(
            conn, integ.id, output_ref=None, candidate_git_sha=integration_sha
        )
        plan = {
            "schema_version": 1,
            "project_human_id": "PRJ-003",
            "iteration_human_id": "ITER-002",
            "sponsor_authority": "approved",
            "jobs": [
                {"human_id": "JOB-P2-PM-SETUP", "queue": "PM", "agent_role": "PM"},
                {
                    "human_id": "JOB-P2-ARCH",
                    "queue": "ARCHITECTURE",
                    "agent_role": "ARCHITECTURE",
                },
                {
                    "human_id": "JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
                    "queue": "DELIVERY",
                    "agent_role": "DELIVERY",
                    "work_item_type": "story",
                    "work_item_human_id": "US-007",
                },
                {
                    "human_id": "JOB-P2-DEL-PRIORITY-FILTER__REWORK-1",
                    "queue": "DELIVERY",
                    "agent_role": "DELIVERY",
                    "work_item_type": "story",
                    "work_item_human_id": "US-008",
                },
                {
                    "human_id": "JOB-P2-INTEGRATION",
                    "queue": "INTEGRATION",
                    "agent_role": "INTEGRATION",
                },
            ],
        }
        conn.execute(
            """
            INSERT INTO pm_plan_runs (
                project_human_id, repository_root, iteration_human_id,
                dry_run, plan_json, status
            ) VALUES (?, ?, ?, 0, ?, 'accepted')
            """,
            (
                "PRJ-003",
                str(repo.resolve()),
                "ITER-002",
                json.dumps(plan),
            ),
        )


@dataclass
class FakeCtl:
    db: Path
    releases: dict = field(default_factory=dict)
    iteration_status: str = "planned"
    calls: list = field(default_factory=list)

    def resolve_db(self, repository_root: Path) -> Path:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        if not self.db.exists() or self.db.stat().st_size == 0:
            self.db.write_bytes(b"sqlite-fake")
        return self.db

    def run(self, repository_root: Path, args: list[str], *, db_path: Path | None = None):
        self.calls.append(args)
        stdout = "ok\n"
        if args[:2] == ["release", "list"]:
            stdout = "human_id\n" + "\n".join(self.releases) + "\n"
        elif args[:2] == ["release", "create"]:
            self.releases["REL-002"] = {"status": "planned", "git_sha": ""}
            stdout = "Created REL-002\n"
        elif args[:2] == ["release", "show"]:
            hid = args[2]
            row = self.releases.get(hid, {"status": "planned", "git_sha": ""})
            stdout = (
                f"human_id: {hid}\nstatus: {row['status']}\ngit_sha: {row.get('git_sha','')}\n"
            )
        elif args[:2] == ["release", "status"]:
            hid, target = args[2], args[3]
            rec = self.releases.setdefault(hid, {"status": "planned", "git_sha": ""})
            rec["status"] = target
            if "--git-sha" in args:
                rec["git_sha"] = args[args.index("--git-sha") + 1]
            stdout = f"Release {hid} status -> {target}\n"
        elif args[:2] == ["iteration", "show"]:
            stdout = f"human_id: {args[2]}\nstatus: {self.iteration_status}\n"
        elif args[:2] == ["iteration", "status"]:
            self.iteration_status = args[3]
            stdout = f"Iteration {args[2]} status -> {args[3]}\n"
        return ProjectctlResult(
            returncode=0,
            stdout=stdout,
            stderr="",
            started_at="t0",
            ended_at="t1",
            duration_ms=1,
            python_executable=Path("python"),
            command=tuple(args),
        )


def test_authoritative_projectctl_db_rejects_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "project-control").mkdir(parents=True)
    (repo / "project-control" / "project.db").write_bytes(b"")
    try:
        resolve_authoritative_projectctl_db(repo)
        raise AssertionError("empty db must fail")
    except Exception as exc:
        assert "empty" in str(exc).lower() or "unusable" in str(exc).lower()


def test_qa_package_from_existing_evidence(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    _seed_lineage(db, repo)
    ev = tmp_path / "ev"
    ev.mkdir()
    with connection(db) as conn:
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE__RETRY-2",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        package, reasons, stories = assemble_qa_package(
            conn,
            rel,
            expected_integration_sha=INTEG_SHA,
            evidence_dir=ev,
            required_story_shas=None,
        )
    assert not reasons
    assert package["integration_sha"] == INTEG_SHA
    assert (ev / "qa-package.json").is_file()
    assert {s.work_item_human_id for s in stories} == {"US-007", "US-008"}


def test_gate_rejects_wrong_integration_sha(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    _seed_lineage(db, repo, integration_sha="aa" * 20)
    ctl = FakeCtl(db=tmp_path / "project-control" / "project.db")
    with connection(db) as conn:
        rel = create_job(
            conn,
            human_id="JOB-REL-BAD-SHA",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        set_job_source_provenance(
            conn,
            rel.id,
            source_delivery_job_id=None,
            source_candidate_sha=INTEG_SHA,
        )
        rel = get_job_by_human_id(conn, rel.human_id)
        result = evaluate_release_job(
            conn,
            rel,
            workspace=repo,
            evidence_root=tmp_path / "ev",
            ops=ctl,
            expected_integration_sha=INTEG_SHA,
            required_story_shas=None,
        )
    assert not result.approved
    assert any("INTEGRATION candidate" in r for r in result.reasons)


def test_release_evaluation_does_not_dirty_workspace(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    sha = current_head_sha(repo)
    _seed_lineage(db, repo, integration_sha=sha)
    cfg = _cfg(tmp_path, repo)
    ctl = FakeCtl(db=tmp_path / "projectctl" / "project.db")
    assert not is_dirty(repo)
    ev = tmp_path / "runs"
    with connection(db) as conn:
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE__RETRY-2",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        set_job_source_provenance(
            conn,
            rel.id,
            source_delivery_job_id=None,
            source_candidate_sha=sha,
        )
        rel = get_job_by_human_id(conn, rel.human_id)
        result = evaluate_release_job(
            conn,
            rel,
            workspace=repo,
            registry_path=cfg,
            evidence_root=ev,
            ops=ctl,
            expected_integration_sha=sha,
            required_story_shas=None,
        )
    assert not is_dirty(repo)
    assert current_head_sha(repo) == sha
    assert result.readiness_report_path.is_file()
    assert str(ev) in str(result.readiness_report_path)
    assert any("release status" in " ".join(c) for c in ctl.calls) or any(
        c[:2] == ["release", "create"] for c in ctl.calls
    )


def test_worker_release_skips_cursor_and_keeps_tree_clean(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    sha = current_head_sha(repo)
    _seed_lineage(db, repo, integration_sha=sha)
    with connection(db) as conn:
        rel = create_job(
            conn,
            human_id="JOB-REL-WORKER",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
            requires_worktree=True,
        )
        set_job_source_provenance(
            conn,
            rel.id,
            source_delivery_job_id=None,
            source_candidate_sha=sha,
        )
        integ = get_job_by_human_id(conn, "JOB-P2-INTEGRATION")
        add_job_dependency(conn, rel.id, integ.id)

    def _eval(conn, job, **kwargs):
        ev = evidence_dir_for_job(job.human_id, run_root=tmp_path / "runs")
        report = ev / "release-readiness.md"
        report.write_text("ok\n", encoding="utf-8")
        return ReleaseEvaluation(
            approved=True,
            reasons=[],
            candidate_sha=sha,
            evidence_dir=ev,
            readiness_report_path=report,
            qa_package_path=None,
            release_human_id="REL-002",
            release_status="qa_passed",
            iteration_status="release_candidate",
            workspace_clean=True,
            workspace_head=sha,
            outcome=GATE_READY_OUTCOME,
        )

    def _cursor(*_a, **_k):
        raise AssertionError("RELEASE must not invoke Cursor against the product tree")

    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-REL-WORKER",
        cursor_runner=_cursor,
        skip_identity_validation=True,
        release_evaluator=_eval,
    )
    assert result.status == "succeeded"
    assert not is_dirty(repo)
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-REL-WORKER")
        assert job.status == "SUCCEEDED"
        assert job.outcome == GATE_READY_OUTCOME
        assert job.candidate_git_sha == sha


def test_gate_rejects_without_qa(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    _seed_lineage(db, repo, qa_result="fail")
    ctl = FakeCtl(db=tmp_path / "pc" / "project.db")
    with connection(db) as conn:
        rel = create_job(
            conn,
            human_id="JOB-REL-NOQA",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        set_job_source_provenance(
            conn,
            rel.id,
            source_delivery_job_id=None,
            source_candidate_sha=INTEG_SHA,
        )
        rel = get_job_by_human_id(conn, rel.human_id)
        result = evaluate_release_job(
            conn,
            rel,
            workspace=repo,
            evidence_root=tmp_path / "ev",
            ops=ctl,
            expected_integration_sha=INTEG_SHA,
            required_story_shas=None,
        )
    assert not result.approved
    assert result.outcome != GATE_READY_OUTCOME


def test_blocked_retry_creates_retry_two(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_lineage(db, repo)
    with connection(db) as conn:
        integ = get_job_by_human_id(conn, "JOB-P2-INTEGRATION")
        orig = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, orig.id, integ.id)
        mark_succeeded(
            conn,
            orig.id,
            output_ref=None,
            candidate_git_sha="56d580d2eca1a634a86990241d4da2958c3323ff",
        )
    r1 = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert r1.successor_job_human_id == "JOB-P2-RELEASE__RETRY-1"
    with connection(db) as conn:
        retry1 = get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1")
        conn.execute(
            "UPDATE orchestration_jobs SET status='BLOCKED', last_error=? WHERE id=?",
            ("dirty worktree", retry1.id),
        )
    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE__RETRY-1", db_path=db, registry_path=cfg
    )
    assert result.ok
    assert result.successor_job_human_id == "JOB-P2-RELEASE__RETRY-2"
    assert result.source_candidate_sha == INTEG_SHA
    with connection(db) as conn:
        orig = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        r1j = get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1")
        r2 = get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-2")
        assert orig.status == "SUCCEEDED"
        assert orig.outcome == "SUPERSEDED"
        assert orig.candidate_git_sha == "56d580d2eca1a634a86990241d4da2958c3323ff"
        assert r1j.status == "BLOCKED"
        assert r1j.superseded_by_job_id == r2.id
        assert r2.status in {"QUEUED", "READY"}
        assert r2.source_candidate_sha == INTEG_SHA
        assert r2.started_at is None


def test_recover_help_still_lists_reconcile_release() -> None:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["recover", "--help"]) == 0
    assert "--reconcile-release" in buf.getvalue()
