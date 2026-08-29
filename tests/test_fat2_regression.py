"""Live Enterprise FAT #2 regression — QA gate, delivery contract, terminal runs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext, emit_projectos_event
from projectos.event_dispatcher import dispatch_event_outbox
from projectos.migrate import initialize_database
from projectos.pm_agent import accept_sponsor_handoff, orchestrate_release_capability
from projectos.qa_gate import emit_qa_gate_evaluation
from projectos.run_evidence import build_terminal_evidence
from projectos.run_state import apply_event_to_run
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_activity_blocks import activity_event_to_blocks
from projectos.sponsor_query import SponsorQueryService
from projectos.qa_semantics import collect_assurance_facts
from projectos.store import add_slack_interface_channel


def _ctx(tmp_path: Path, *, with_delivery_json: bool = True) -> ServiceContext:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    if with_delivery_json:
        (repo / "project").mkdir(exist_ok=True)
        (repo / "project" / "delivery.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "delivery_type": "desktop_application",
                    "target_platforms": ["windows-x64"],
                    "packaging_adapter": "generic",
                    "repository_provider": "github",
                    "repository_owner": "acme",
                    "repository_name": "gamma",
                    "default_branch": "main",
                    "release_strategy": "semantic_version",
                    "installer_format": "exe",
                    "installer_name_template": "{product}-Setup-{version}.exe",
                    "artifact_retention": 10,
                    "code_signing_policy": "not_required",
                    "sbom_policy": "required",
                    "checksum_policy": "sha256",
                    "github_release_enabled": False,
                    "slack_release_announcement_enabled": False,
                    "trusted_build_command": "echo build",
                }
            ),
            encoding="utf-8",
        )
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id="C0BSYCCDRST", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _seed_qa_evidence(conn, *, total: int = 16, failed: int = 8, repo_root: str = "/repo") -> None:
    for i in range(total):
        result = "fail" if i < failed else "pass"
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result, created_at
            ) VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            ("PRJ-003", repo_root, f"sha{i:04d}", f"ASSURANCE_{i % 4}", result),
        )


def _seed_handoff_run(conn, *, run_id: str = "RUN-29280E59", handoff_id: str = "HND-99383CEFF5F7") -> EventContext:
    conn.execute(
        """
        INSERT INTO sponsor_handoffs (
            handoff_id, project_id, team_id, channel_id, thread_ts,
            sponsor_user_id, request_type, objective, status, run_id
        ) VALUES (?, 'PRJ-003', 'T1', 'C0BSYCCDRST', '1788023487.700189', 'U1', 'RELEASE',
                  're-release package and installer', 'ACCEPTED_BY_PM', ?)
        """,
        (handoff_id, run_id),
    )
    conn.execute(
        """
        INSERT INTO execution_runs (
            run_id, project_id, handoff_id, request_type, objective, status
        ) VALUES (?, 'PRJ-003', ?, 'RELEASE', 're-release package and installer', 'PLANNING')
        """,
        (run_id, handoff_id),
    )
    return EventContext(
        project_id="PRJ-003",
        handoff_id=handoff_id,
        run_id=run_id,
        slack_channel_id="C0BSYCCDRST",
        slack_thread_ts="1788023487.700189",
    )


def _release_handoff() -> HandoffRequest:
    return HandoffRequest(
        project_id="PRJ-003",
        objective="re-release package and installer",
        action_type="prepare_release",
        rationale="",
        scope="",
        constraints="",
        acceptance_intent="",
        exclusions="",
        source_conversation_summary="",
    )


def test_qa_gate_failed_blocks_delivery_phase(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=8)
        event_ctx = _seed_handoff_run(conn)
        handoff = _release_handoff()
        conn.commit()
        evidence = orchestrate_release_capability(
            ctx, event_ctx=event_ctx, project_id="PRJ-003", handoff=handoff
        )
        events = conn.execute(
            "SELECT event_type, phase FROM projectos_events WHERE run_id = 'RUN-29280E59'"
        ).fetchall()
        run = conn.execute(
            "SELECT status, current_phase FROM execution_runs WHERE run_id = 'RUN-29280E59'"
        ).fetchone()
        terminal = build_terminal_evidence(conn, run_id="RUN-29280E59")
    types = [r["event_type"] for r in events]
    phase_events = [r for r in events if r["event_type"] == "PHASE_CHANGED"]
    assert "QA_GATE_FAILED" in types
    assert "RUN_BLOCKED" in types
    assert "AGENT_ASSIGNED" not in types
    assert all(
        "RELEASE_PREPARATION" not in str(r.get("phase") or "").upper()
        and "DELIVERY" not in str(r.get("phase") or "").upper()
        for r in phase_events
    )
    assert run["status"] == "BLOCKED"
    assert "RUN BLOCKED" in evidence.upper()
    assert terminal.get("failure")


def test_terminal_run_not_reopened_by_later_events(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_handoff_run(conn, run_id="RUN-LOCK")
        apply_event_to_run(
            conn,
            run_id="RUN-LOCK",
            event_type="QA_GATE_FAILED",
            payload={"status": "BLOCKED", "phase": "QA_GATE", "actor_id": "qa-agent", "progress": 40},
        )
        apply_event_to_run(
            conn,
            run_id="RUN-LOCK",
            event_type="PHASE_CHANGED",
            payload={"phase": "DELIVERY", "actor_id": "pm-agent", "progress": 25},
        )
        apply_event_to_run(
            conn,
            run_id="RUN-LOCK",
            event_type="AGENT_ASSIGNED",
            payload={"phase": "DELIVERY", "actor_id": "delivery-agent", "progress": 25},
        )
        run = conn.execute(
            "SELECT status, current_phase FROM execution_runs WHERE run_id = 'RUN-LOCK'"
        ).fetchone()
    assert run["status"] == "BLOCKED"
    assert run["current_phase"] == "QA_GATE"


def test_handoff_accepted_outbox_includes_request_type(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                handoff_id="HND-1",
                run_id="RUN-1",
                slack_channel_id="C1",
                slack_thread_ts="1.0",
            ),
            event_type="HANDOFF_ACCEPTED",
            summary="release package",
            metadata={"request_type": "RELEASE"},
        )
        row = conn.execute(
            "SELECT payload_json FROM event_outbox WHERE subscriber = 'slack' LIMIT 1"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    blocks = activity_event_to_blocks(payload)
    block_text = json.dumps(blocks)
    assert payload.get("request_type") == "RELEASE"
    assert "RELEASE" in block_text


def test_qa_pass_missing_delivery_contract_blocks_with_evidence(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, with_delivery_json=False)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=0)
        event_ctx = _seed_handoff_run(conn, run_id="RUN-DELIV")
        handoff = _release_handoff()
        conn.commit()
        evidence = orchestrate_release_capability(
            ctx, event_ctx=event_ctx, project_id="PRJ-003", handoff=handoff
        )
        events = conn.execute(
            "SELECT event_type, phase, summary FROM projectos_events WHERE run_id = 'RUN-DELIV'"
        ).fetchall()
        terminal = build_terminal_evidence(conn, run_id="RUN-DELIV")
    types = {r["event_type"] for r in events}
    assert "QA_GATE_PASSED" in types
    assert "RELEASE_PREPARATION_BLOCKED" in types
    assert "RUN_BLOCKED" in types
    failure = terminal.get("failure") or {}
    assert failure.get("blocker_type") == "DELIVERY_CONTRACT_MISSING"
    assert "delivery.json" in str(failure.get("path", "")).lower()
    assert "DELIVERY_CONTRACT_MISSING" in evidence or "RUN BLOCKED" in evidence.upper()


def test_blocker_question_uses_terminal_evidence(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, with_delivery_json=False)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=8)
        event_ctx = _seed_handoff_run(conn, run_id="RUN-BLK")
        handoff = _release_handoff()
        conn.commit()
        orchestrate_release_capability(ctx, event_ctx=event_ctx, project_id="PRJ-003", handoff=handoff)
    answer = SponsorQueryService(ctx).get_blocker_summary("PRJ-003")
    assert "delivery.json" in answer.lower() or "QA gate" in answer
    assert "Required action" in answer or "Blocker type" in answer or "Reason:" in answer


def test_qa_semantics_preserve_distinct_job_and_review_counts(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=0)
        for i in range(20):
            conn.execute(
                """
                INSERT INTO orchestration_jobs (
                    human_id, project_human_id, repository_root, queue, status,
                    agent_role, created_at, updated_at
                ) VALUES (?, 'PRJ-003', '/repo', 'ASSURANCE_FUNCTIONAL', 'SUCCEEDED',
                          'ASSURANCE', datetime('now'), datetime('now'))
                """,
                (f"JOB-A{i:03d}",),
            )
    facts = collect_assurance_facts(ctx, "PRJ-003")
    assert facts.get("qa_jobs_total") == 20
    assert facts.get("reviews_total") == 16
    assert facts.get("qa_jobs_total") != facts.get("reviews_total")
    rules = facts.get("semantic_rules", {})
    assert "never_substitute" in rules


def test_accept_handoff_emits_terminal_on_orchestration_failure(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            __import__("projectos.errors", fromlist=["OrchestrationError"]).OrchestrationError("unexpected")
        ),
    )
    handoff = _release_handoff()
    with connection(ctx.db_path) as conn:
        with pytest.raises(Exception):
            accept_sponsor_handoff(
                ctx,
                conn,
                handoff=handoff,
                project_id="PRJ-003",
                team_id="T1",
                channel_id="C0BSYCCDRST",
                thread_ts="1788023487.700189",
                sponsor_user_id="U1",
            )
        run = conn.execute(
            "SELECT status FROM execution_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert run is not None
    assert run["status"] in {"BLOCKED", "FAILED"}
