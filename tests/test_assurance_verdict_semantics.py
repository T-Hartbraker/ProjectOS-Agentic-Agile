"""Assurance execution vs verdict semantics — worker success is not QA PASS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakes.orchestration_fakes import (
    SequencedAssuranceExecutor,
    make_git_remediation_worker,
)
from helpers import init_git_repo, write_identity, write_registry
from projectos.assurance_verdict import (
    ASSURANCE_RESULT_MARKER,
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASS,
    AssuranceValidationError,
    assurance_result_for_test,
    format_assurance_stdout,
    parse_and_validate_assurance_result,
    verdict_to_evidence_result,
)
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.pm_remediation import run_qa_with_remediation
from projectos.qa_gate import collect_qa_gate_facts
from projectos.qa_handoff import (
    process_assurance_worker_success,
    record_assurance_result,
)
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
from projectos.store import (
    create_job,
    get_job,
    insert_qa_evidence,
    mark_succeeded,
    set_job_source_provenance,
)


def _db(tmp_path: Path) -> tuple[Path, str]:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    repo_root = str(repo.resolve())
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": repo_root, "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db, repo_root


def _assurance_job(
    conn,
    repo_root: str,
    *,
    candidate: str = "shaA",
    human_id: str = "QA-FUNC-1",
    delivery_human_id: str = "DEL-1",
    queue: str = "ASSURANCE_FUNCTIONAL",
) -> tuple[object, int]:
    delivery = create_job(
        conn,
        human_id=delivery_human_id,
        project_human_id="PRJ-003",
        repository_root=repo_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="SUCCEEDED",
        base_git_sha=candidate,
    )
    conn.execute(
        "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
        (candidate, delivery.id),
    )
    assurance = create_job(
        conn,
        human_id=human_id,
        project_human_id="PRJ-003",
        repository_root=repo_root,
        agent_role=queue,
        queue=queue,
        status="SUCCEEDED",
        base_git_sha=candidate,
    )
    set_job_source_provenance(
        conn,
        assurance.id,
        source_delivery_job_id=delivery.id,
        source_candidate_sha=candidate,
    )
    assurance = get_job(conn, assurance.id)
    insert_qa_evidence(
        conn,
        project_human_id="PRJ-003",
        repository_root=repo_root,
        delivery_job_id=delivery.id,
        assurance_job_id=assurance.id,
        candidate_git_sha=candidate,
        assurance_role=queue,
        result="pending",
    )
    return assurance, delivery.id


def _stdout_for(assurance, *, verdict: str, **overrides) -> str:
    result = assurance_result_for_test(
        verdict=verdict,
        assurance=assurance,
        summary=f"test {verdict}",
        findings=[
            {
                "finding_id": "FND-1",
                "category": "SOURCE_CODE_DEFECT",
                "severity": "high",
                "evidence": "broken",
                "affected_component": "module",
                "expected_condition": "ok",
                "actual_condition": "broken",
                "recommended_owner_role": "SOURCE_CODE_DEFECT",
            }
        ]
        if verdict == VERDICT_FAIL
        else None,
    )
    payload = result.to_dict()
    payload.update(overrides)
    return f"{ASSURANCE_RESULT_MARKER}\n{json.dumps(payload)}"


def _seed_run(conn) -> EventContext:
    handoff = create_sponsor_handoff(
        conn,
        project_id="PRJ-003",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        sponsor_user_id="U1",
        request_type="RELEASE",
        objective="ship",
    )
    run = create_execution_run(
        conn,
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective="ship",
    )
    mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=run.run_id)
    update_execution_run(conn, run_id=run.run_id, status="RUNNING")
    return EventContext(project_id="PRJ-003", handoff_id=handoff.handoff_id, run_id=run.run_id)


def _seed_qa(conn, *, candidate: str, failed: int, total: int, repo_root: str, run_id: str) -> None:
    for i in range(total):
        result = "fail" if i < failed else "pass"
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result, run_id
            ) VALUES ('PRJ-003', ?, ?, ?, ?, ?)
            """,
            (repo_root, candidate, f"ASSURANCE_{i % 4}", result, run_id),
        )


def test_verdict_to_evidence_result_mapping() -> None:
    assert verdict_to_evidence_result(VERDICT_PASS) == "pass"
    assert verdict_to_evidence_result(VERDICT_FAIL) == "fail"
    assert verdict_to_evidence_result(VERDICT_INCONCLUSIVE) == "inconclusive"
    with pytest.raises(AssuranceValidationError):
        verdict_to_evidence_result("MAYBE")


def test_parse_and_validate_pass(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        stdout = _stdout_for(assurance, verdict=VERDICT_PASS)
        result = parse_and_validate_assurance_result(stdout, assurance)
    assert result.verdict == VERDICT_PASS


def test_execution_success_with_pass_verdict(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        mark_succeeded(conn, assurance.id, output_ref="out", candidate_git_sha="shaA")
        evidence = process_assurance_worker_success(
            conn,
            assurance,
            stdout=_stdout_for(assurance, verdict=VERDICT_PASS),
            evidence_ref="out",
        )
        row = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (assurance.id,),
        ).fetchone()
        facts = collect_qa_gate_facts(conn, project_id="PRJ-003", candidate_git_sha="shaA")
    assert evidence == "pass"
    assert row["result"] == "pass"
    assert facts["gate"] == "PASSED"


def test_execution_success_with_fail_verdict(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        mark_succeeded(conn, assurance.id, output_ref="out", candidate_git_sha="shaA")
        evidence = process_assurance_worker_success(
            conn,
            assurance,
            stdout=_stdout_for(assurance, verdict=VERDICT_FAIL),
            evidence_ref="out",
        )
        rework = conn.execute(
            "SELECT human_id FROM orchestration_jobs WHERE human_id LIKE '%__REWORK'"
        ).fetchone()
        facts = collect_qa_gate_facts(conn, project_id="PRJ-003", candidate_git_sha="shaA")
    assert evidence == "fail"
    assert rework is None
    assert facts["gate"] == "FAILED"


def test_execution_success_with_inconclusive_verdict(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        mark_succeeded(conn, assurance.id, output_ref="out", candidate_git_sha="shaA")
        evidence = process_assurance_worker_success(
            conn,
            assurance,
            stdout=_stdout_for(assurance, verdict=VERDICT_INCONCLUSIVE),
            evidence_ref="out",
        )
        rework = conn.execute(
            "SELECT human_id FROM orchestration_jobs WHERE human_id LIKE '%__REWORK'"
        ).fetchone()
        facts = collect_qa_gate_facts(conn, project_id="PRJ-003", candidate_git_sha="shaA")
    assert evidence == "inconclusive"
    assert rework is None
    assert facts["gate"] == "INCONCLUSIVE"


def test_execution_success_with_malformed_result(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        mark_succeeded(conn, assurance.id, output_ref="out", candidate_git_sha="shaA")
        evidence = process_assurance_worker_success(
            conn,
            assurance,
            stdout="looks good, all tests passed",
            evidence_ref="out",
        )
        row = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (assurance.id,),
        ).fetchone()
        facts = collect_qa_gate_facts(conn, project_id="PRJ-003", candidate_git_sha="shaA")
    assert evidence == "inconclusive"
    assert row["result"] == "inconclusive"
    assert facts["gate"] != "PASSED"


def test_execution_failure_does_not_fabricate_verdict(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        row = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (assurance.id,),
        ).fetchone()
    assert row["result"] == "pending"


def test_wrong_candidate_rejected(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        stdout = _stdout_for(assurance, verdict=VERDICT_PASS, candidate_id="shaWRONG")
        evidence = process_assurance_worker_success(conn, assurance, stdout=stdout, evidence_ref="out")
        row = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (assurance.id,),
        ).fetchone()
    assert evidence == "inconclusive"
    assert row["result"] == "inconclusive"


def test_wrong_job_id_rejected(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        stdout = _stdout_for(assurance, verdict=VERDICT_PASS, assurance_job_id="WRONG-JOB")
        evidence = process_assurance_worker_success(conn, assurance, stdout=stdout, evidence_ref="out")
        row = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (assurance.id,),
        ).fetchone()
    assert evidence == "inconclusive"
    assert row["result"] != "pass"


def test_stale_pass_does_not_validate_active_candidate(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance_a, delivery_id = _assurance_job(conn, repo_root, candidate="shaA")
        process_assurance_worker_success(
            conn,
            assurance_a,
            stdout=_stdout_for(assurance_a, verdict=VERDICT_PASS),
            evidence_ref="a",
        )
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
            ("shaB", delivery_id),
        )
        _assurance_job(
            conn,
            repo_root,
            candidate="shaB",
            human_id="QA-FUNC-2",
            delivery_human_id="DEL-2",
        )
        with pytest.raises(Exception):
            record_assurance_result(conn, assurance_a, verdict=VERDICT_PASS, evidence_ref="stale")
        facts_b = collect_qa_gate_facts(conn, project_id="PRJ-003", candidate_git_sha="shaB")
    assert facts_b["gate"] != "PASSED"


def test_closed_loop_fail_then_pass(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([False, True])
    with connection(db) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, candidate="shaA", failed=4, total=4, repo_root=repo_root, run_id=event_ctx.run_id)
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
            max_cycles=3,
        )
    assert result.gate == "PASSED"
    assert len(assurance.calls) == 2


def test_inconclusive_gate_blocks_pm_remediation(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        event_ctx = _seed_run(conn)
        delivery = create_job(
            conn,
            human_id="DEL-INCONCLUSIVE",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            base_git_sha="shaA",
            run_id=event_ctx.run_id,
        )
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
            ("shaA", delivery.id),
        )
        for role in ("ASSURANCE_FUNCTIONAL", "ASSURANCE_INTEGRATION", "ASSURANCE_SECURITY", "ASSURANCE_QUALITY"):
            conn.execute(
                """
                INSERT INTO qa_evidence (
                    project_human_id, repository_root, delivery_job_id, candidate_git_sha,
                    assurance_role, result, run_id
                ) VALUES ('PRJ-003', ?, ?, 'shaA', ?, 'inconclusive', ?)
                """,
                (repo_root, delivery.id, role, event_ctx.run_id),
            )
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            max_cycles=2,
        )
        remediation = conn.execute(
            "SELECT 1 FROM remediation_work WHERE run_id = ?",
            (event_ctx.run_id,),
        ).fetchone()
    assert result.gate == "INCONCLUSIVE"
    assert remediation is None


def test_format_assurance_stdout_roundtrip(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        assurance, _ = _assurance_job(conn, repo_root)
        result = assurance_result_for_test(verdict=VERDICT_PASS, assurance=assurance)
        stdout = format_assurance_stdout(result)
        parsed = parse_and_validate_assurance_result(stdout, assurance)
    assert parsed.verdict == VERDICT_PASS
