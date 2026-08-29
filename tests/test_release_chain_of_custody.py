"""Full release chain-of-custody and negative terminal integrity tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from control_plane_helpers import (
    assert_run_not_completed,
    count_events,
    create_release_handoff,
    delivery_json,
    git_commit_file,
    git_head,
    git_parent,
    run_full_qa_for_candidate,
    setup_release_project,
)
from fakes.orchestration_fakes import (
    SequencedAssuranceExecutor,
    git_commit_file as fake_git_commit,
    make_git_remediation_worker,
)
from projectos.acceptance_contract import build_acceptance_contract, evaluate_effective_requirements
from projectos.candidate_model import set_run_active_candidate
from projectos.candidate_workspace import candidate_workspace
from projectos.db import connection
from projectos.delivery.gates import GATE_STATUS_PASSED
from projectos.delivery.service import DeliveryService
from projectos.delivery.store import (
    get_delivery_release,
    insert_delivery_artifact,
    insert_delivery_release,
    list_delivery_artifacts,
    new_artifact_id,
    new_release_record_id,
    update_delivery_artifact,
    update_delivery_release,
    upsert_gate_status,
)
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.execution_run import get_execution_run
from projectos.github.client import GitHubClient
from projectos.github.tokens import reload_github_tokens
from projectos.pm_remediation import run_qa_with_remediation
from projectos.qa_handoff import create_assurance_jobs_for_delivery, record_assurance_result
from projectos.qa_manager import execute_qa_manager_aggregation
from projectos.sponsor_outcome import evaluate_sponsor_outcome
from projectos.store import create_job, get_job, mark_succeeded


def _github_mock():
    def fake_post(url, headers, body=None, method="POST"):
        if url.endswith("/repos/acme/example-product") and method == "GET":
            return {"id": 1, "full_name": "acme/example-product", "default_branch": "main"}
        if url.endswith("/releases") and method == "POST":
            return {
                "id": 2,
                "html_url": "https://github.com/acme/example-product/releases/tag/v1.0.0",
                "upload_url": "https://upload.example/{?name,label}",
            }
        if "upload.example" in url:
            name = url.split("name=")[-1]
            return {"id": 3, "browser_download_url": f"https://github.com/download/{name}"}
        if "/releases/tags/" in url and method == "GET":
            return {"message": "Not Found"}
        return {"ok": True}

    return GitHubClient(http_post=fake_post, token="ghp_testtoken1234567890")


def _delivery_job(conn, *, project_id: str, repo_root: str, candidate_sha: str, run_id: str, human_id: str | None = None):
    delivery = create_job(
        conn,
        human_id=human_id or f"DEL-{run_id[-8:]}",
        project_human_id=project_id,
        repository_root=repo_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="SUCCEEDED",
        base_git_sha=git_parent(Path(repo_root)),
        run_id=run_id,
    )
    conn.execute(
        "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
        (candidate_sha, delivery.id),
    )
    return get_job(conn, delivery.id)


def test_release_chain_of_custody_and_terminal_truth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_GITHUB_TOKEN", "ghp_testtoken1234567890")
    reload_github_tokens()
    ctx, repo, candidate_a = setup_release_project(tmp_path, github_release_enabled=True)
    repo_path = Path(repo)
    (repo_path / "project" / "delivery.json").write_text(
        json.dumps(
            delivery_json(
                github_release_enabled=True,
                code_signing_policy="not_required",
                installer_format="zip",
            )
        ),
        encoding="utf-8",
    )
    project_id = "PRJ-004"
    with connection(ctx.db_path) as conn:
        handoff, run, event_ctx = create_release_handoff(conn, project_id=project_id)
        assert_run_not_completed(conn, run.run_id)
        run_full_qa_for_candidate(
            conn,
            repo_root=str(repo),
            project_id=project_id,
            run_id=run.run_id,
            candidate_sha=candidate_a,
        )
        set_run_active_candidate(conn, run_id=run.run_id, candidate_id=candidate_a)
        set_run_active_candidate(conn, run_id=run.run_id, candidate_id=candidate_a)

    svc = DeliveryService(ctx, github_client=_github_mock())
    prepared = svc.prepare_release(
        project_id,
        release_human_id="REL-CHAIN-001",
        version="1.0.0",
        candidate_git_sha=candidate_a,
        run_id=run.run_id,
        event_context=event_ctx,
    )
    record_id = prepared["release_record_id"]
    assert prepared["candidate_git_sha"] == candidate_a

    with candidate_workspace(repo_path, candidate_a, parent_dir=tmp_path / "ws-proof") as workspace:
        ws_head = git_head(workspace)
        assert ws_head == candidate_a
        git_commit_file(repo_path, "src/mutate-after-ws.txt", "mutated\n")
        ws_head_after = git_head(workspace)
        assert ws_head_after == candidate_a

    packaged = svc.package_release(record_id, event_context=event_ctx)
    artifacts = packaged["artifacts"]
    zip_art = next(item for item in artifacts if item["artifact_type"] == "zip")
    assert zip_art["source_git_sha"] == candidate_a
    assert zip_art["sha256"]
    assert zip_art["size_bytes"] > 0
    assert Path(zip_art["local_build_path"]).is_file()

    verified = svc.verify_release(record_id)
    assert verified["gate_summary"]["VERIFY_GATE"] == GATE_STATUS_PASSED

    with connection(ctx.db_path) as conn:
        pre_publish = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish release",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha=candidate_a,
            project_id=project_id,
            registry_path=ctx.registry_path,
        )
        assert pre_publish.satisfied is False
        assert_run_not_completed(conn, run.run_id)

    published = svc.publish_release(record_id, event_context=event_ctx)
    assert published["publication_status"] == "published"
    assert published["github_release_url"]

    with connection(ctx.db_path) as conn:
        contract = build_acceptance_contract(
            conn,
            handoff_id=handoff.handoff_id,
            request_type="RELEASE",
            objective="publish release",
        )
        satisfied, required, ok, missing, _ = evaluate_effective_requirements(
            conn,
            contract=contract,
            release_record_id=record_id,
            candidate_git_sha=candidate_a,
        )
        assert satisfied is True
        assert "candidate_provenance" in required
        assert "publication" in required
        assert not missing
        final_run = get_execution_run(conn, run.run_id)
        assert final_run is not None
        assert final_run.status == "COMPLETED"
        assert count_events(conn, run_id=run.run_id, event_type="RUN_COMPLETED") == 1


def test_negative_candidate_provenance_mismatch(tmp_path: Path) -> None:
    ctx, repo, candidate_a = setup_release_project(tmp_path)
    with connection(ctx.db_path) as conn:
        _, run, _ = create_release_handoff(conn, project_id="PRJ-004")
        record_id = new_release_record_id()
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-004",
            release_human_id="REL-NEG-1",
            version="1.0.0",
            candidate_git_sha="sha-b",
            lifecycle_status="verified",
        )
        evaluation = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=None,
            objective="publish release",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha=candidate_a,
            project_id="PRJ-004",
            registry_path=ctx.registry_path,
        )
    assert evaluation.satisfied is False
    assert "candidate_provenance" in evaluation.missing_outputs


def test_negative_artifact_source_sha_mismatch(tmp_path: Path) -> None:
    ctx, _, candidate_a = setup_release_project(tmp_path)
    record_id = new_release_record_id()
    with connection(ctx.db_path) as conn:
        _, run, _ = create_release_handoff(conn, project_id="PRJ-004")
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-004",
            release_human_id="REL-NEG-2",
            version="1.0.0",
            candidate_git_sha=candidate_a,
            lifecycle_status="verified",
        )
        insert_delivery_artifact(
            conn,
            artifact_id=new_artifact_id(),
            release_record_id=record_id,
            project_human_id="PRJ-004",
            artifact_name="bad.zip",
            artifact_type="zip",
            platform="windows-x64",
            architecture="x64",
            version="1.0.0",
            source_git_sha="sha-other",
            build_id="BLD-1",
            build_timestamp="2026-01-01T00:00:00Z",
            local_build_path=str(tmp_path / "bad.zip"),
            sha256="abc",
            size_bytes=3,
            signature_status="not_required",
        )
        Path(tmp_path / "bad.zip").write_bytes(b"bad")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="VERIFY_GATE", status="passed")
        evaluation = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=None,
            objective="publish release",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha=candidate_a,
            project_id="PRJ-004",
            registry_path=ctx.registry_path,
        )
    assert evaluation.satisfied is False


def test_negative_publish_without_verify_gate(tmp_path: Path) -> None:
    ctx, _, candidate_a = setup_release_project(tmp_path)
    svc = DeliveryService(ctx)
    record_id = new_release_record_id()
    with connection(ctx.db_path) as conn:
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-004",
            release_human_id="REL-NEG-3",
            version="1.0.0",
            candidate_git_sha=candidate_a,
            lifecycle_status="packaged",
        )
        upsert_gate_status(conn, release_record_id=record_id, gate_name="PACKAGE_GATE", status="passed")
    with pytest.raises(OrchestrationError, match="verification"):
        svc.publish_release(record_id)


def test_negative_tampered_artifact_fails_verify(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_GITHUB_TOKEN", "")
    reload_github_tokens()
    ctx, repo, candidate_a = setup_release_project(tmp_path)
    (Path(repo) / "project" / "delivery.json").write_text(
        json.dumps(delivery_json(installer_format="zip", github_release_enabled=False)),
        encoding="utf-8",
    )
    with connection(ctx.db_path) as conn:
        _, run, event_ctx = create_release_handoff(conn, project_id="PRJ-004")
        run_full_qa_for_candidate(
            conn, repo_root=str(repo), project_id="PRJ-004", run_id=run.run_id, candidate_sha=candidate_a
        )
    svc = DeliveryService(ctx)
    prepared = svc.prepare_release(
        "PRJ-004",
        release_human_id="REL-TAMPER",
        version="1.0.0",
        candidate_git_sha=candidate_a,
        run_id=run.run_id,
        event_context=event_ctx,
    )
    record_id = prepared["release_record_id"]
    svc.package_release(record_id, event_context=event_ctx)
    with connection(ctx.db_path) as conn:
        art = list_delivery_artifacts(conn, record_id)[0]
        path = Path(str(art["local_build_path"]))
        path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(OrchestrationError, match="checksum"):
        svc.verify_release(record_id)
    with connection(ctx.db_path) as conn:
        assert_run_not_completed(conn, run.run_id)


def test_negative_publication_without_url_rejected(tmp_path: Path) -> None:
    ctx, _, _ = setup_release_project(tmp_path)
    record_id = new_release_record_id()
    with connection(ctx.db_path) as conn:
        _, run, _ = create_release_handoff(conn, project_id="PRJ-004")
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-004",
            release_human_id="REL-NEG-5",
            version="1.0.0",
            candidate_git_sha="sha-a",
            lifecycle_status="verified",
        )
        update_delivery_release(conn, record_id, publication_status="published")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="VERIFY_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="QA_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="PUBLICATION_GATE", status="passed")
        with pytest.raises(OrchestrationError, match="publication"):
            emit_projectos_event(
                conn,
                ctx=EventContext(project_id="PRJ-004", run_id=run.run_id),
                event_type="RELEASE_PUBLISHED",
                summary="spoof",
                actor_id=ACTOR_PM,
                evidence={"release_record_id": record_id, "url": ""},
            )


def test_qa_failure_remediates_new_candidate_and_retests(tmp_path: Path) -> None:
    ctx, repo, candidate_a = setup_release_project(tmp_path, github_release_enabled=False)
    repo_root = str(repo)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([True])
    with connection(ctx.db_path) as conn:
        _, run, event_ctx = create_release_handoff(conn, project_id="PRJ-004")
        delivery = _delivery_job(
            conn, project_id="PRJ-004", repo_root=repo_root, candidate_sha=candidate_a, run_id=run.run_id
        )
        handoff = create_assurance_jobs_for_delivery(conn, delivery, candidate_git_sha=candidate_a)
        role_verdicts = {
            "ASSURANCE_FUNCTIONAL": "PASS",
            "ASSURANCE_INTEGRATION": "PASS",
            "ASSURANCE_SECURITY": "FAIL",
            "ASSURANCE_QUALITY": "PASS",
        }
        for hid in handoff.assurance_job_ids:
            if "QA_MANAGER" in hid:
                continue
            row = conn.execute("SELECT id, queue FROM orchestration_jobs WHERE human_id = ?", (hid,)).fetchone()
            assurance_job = get_job(conn, int(row["id"]))
            verdict = role_verdicts.get(str(row["queue"]), "PASS")
            findings = None
            if verdict == "FAIL":
                findings = [
                    {
                        "finding_id": "FND-SEC-1",
                        "category": "SECURITY_FINDING",
                        "severity": "high",
                        "evidence": "security issue",
                        "affected_component": "auth",
                        "expected_condition": "secure",
                        "actual_condition": "insecure",
                        "recommended_owner_role": "SECURITY_FINDING",
                    }
                ]
            record_assurance_result(conn, assurance_job, verdict=verdict, evidence_ref="e", findings=findings)
            conn.execute(
                "UPDATE qa_evidence SET run_id = ? WHERE assurance_job_id = ?",
                (run.run_id, assurance_job.id),
            )
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-004",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
        )
        shas = {
            str(row["candidate_git_sha"])
            for row in conn.execute(
                "SELECT DISTINCT candidate_git_sha FROM qa_evidence WHERE run_id = ?",
                (run.run_id,),
            ).fetchall()
        }
    assert result.gate == "PASSED"
    assert candidate_a in shas
    candidate_b_sha = next(c for c in shas if c != candidate_a)

    svc = DeliveryService(ctx)
    prepared = svc.prepare_release(
        "PRJ-004",
        release_human_id="REL-REMED",
        version="1.0.0",
        candidate_git_sha=candidate_b_sha,
        run_id=run.run_id,
        event_context=event_ctx,
    )
    assert prepared["candidate_git_sha"] == candidate_b_sha
    assert prepared["candidate_git_sha"] != candidate_a


def test_qa_inconclusive_retries_same_candidate_until_verdict(tmp_path: Path) -> None:
    from projectos.qa_inconclusive import schedule_assurance_retry_for_inconclusive
    from projectos.run_next_actions import list_active_next_actions

    ctx, repo, candidate_a = setup_release_project(tmp_path, github_release_enabled=False)
    repo_root = str(repo)
    with connection(ctx.db_path) as conn:
        _, run, event_ctx = create_release_handoff(conn, project_id="PRJ-004")
        delivery = _delivery_job(
            conn, project_id="PRJ-004", repo_root=repo_root, candidate_sha=candidate_a, run_id=run.run_id
        )
        handoff = create_assurance_jobs_for_delivery(conn, delivery, candidate_git_sha=candidate_a)
        for hid in handoff.assurance_job_ids:
            if "QA_MANAGER" in hid:
                continue
            row = conn.execute("SELECT id, queue FROM orchestration_jobs WHERE human_id = ?", (hid,)).fetchone()
            job = get_job(conn, int(row["id"]))
            verdict = "INCONCLUSIVE" if row["queue"] == "ASSURANCE_SECURITY" else "PASS"
            record_assurance_result(conn, job, verdict=verdict, evidence_ref="e")
            conn.execute(
                "UPDATE qa_evidence SET run_id = ? WHERE assurance_job_id = ?",
                (run.run_id, job.id),
            )

        schedule_assurance_retry_for_inconclusive(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-004",
            repository_root=repo_root,
            candidate_git_sha=candidate_a,
            run_id=run.run_id,
            inconclusive_roles=["ASSURANCE_SECURITY"],
        )
        retry1 = conn.execute(
            "SELECT human_id FROM orchestration_jobs WHERE human_id LIKE '%__RETRY_001'"
        ).fetchone()
        assert retry1 is not None
        retry_job = get_job(conn, int(conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?", (retry1["human_id"],)
        ).fetchone()["id"]))
        record_assurance_result(conn, retry_job, verdict="INCONCLUSIVE", evidence_ref="retry1")
        schedule_assurance_retry_for_inconclusive(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-004",
            repository_root=repo_root,
            candidate_git_sha=candidate_a,
            run_id=run.run_id,
            inconclusive_roles=["ASSURANCE_SECURITY"],
        )
        retry2 = conn.execute(
            "SELECT human_id FROM orchestration_jobs WHERE human_id LIKE '%__RETRY_002'"
        ).fetchone()
        assert retry2 is not None
        retry_job2 = get_job(conn, int(conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?", (retry2["human_id"],)
        ).fetchone()["id"]))
        record_assurance_result(conn, retry_job2, verdict="PASS", evidence_ref="retry2")
        mgr = conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?",
            (f"{delivery.human_id}__QA_MANAGER",),
        ).fetchone()
        mgr_job = get_job(conn, int(mgr["id"]))
        mark_succeeded(conn, mgr_job.id, output_ref="mgr", candidate_git_sha=candidate_a)
        execute_qa_manager_aggregation(conn, mgr_job)
        retry_ids = [
            row["human_id"]
            for row in conn.execute(
                "SELECT human_id FROM orchestration_jobs WHERE human_id LIKE '%__RETRY_%' ORDER BY human_id"
            ).fetchall()
        ]
        remediation = conn.execute(
            "SELECT COUNT(*) FROM remediation_work WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
    assert "RETRY_001" in retry_ids[0]
    assert "RETRY_002" in retry_ids[1]
    assert int(remediation) == 0
