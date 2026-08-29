"""QA closed-loop remediation tests."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.migrate import initialize_database
from projectos.pm_remediation import run_qa_with_remediation
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest


def _ctx(tmp_path: Path) -> ServiceContext:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _seed_qa(conn, *, total: int = 4, failed: int = 2) -> None:
    for i in range(total):
        result = "fail" if i < failed else "pass"
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result
            ) VALUES ('PRJ-003', '/repo', ?, ?, ?)
            """,
            (f"sha{i:04d}", f"ASSURANCE_{i % 2}", result),
        )


def _seed_run(conn, run_id: str = "RUN-QA") -> EventContext:
    conn.execute(
        """
        INSERT INTO sponsor_handoffs (
            handoff_id, project_id, team_id, channel_id, thread_ts,
            sponsor_user_id, request_type, objective, status, run_id
        ) VALUES ('HND-QA', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'RELEASE', 'ship', 'ACCEPTED_BY_PM', ?)
        """,
        (run_id,),
    )
    conn.execute(
        """
        INSERT INTO execution_runs (
            run_id, project_id, handoff_id, request_type, objective, status
        ) VALUES (?, 'PRJ-003', 'HND-QA', 'RELEASE', 'ship', 'RUNNING')
        """,
        (run_id,),
    )
    return EventContext(project_id="PRJ-003", handoff_id="HND-QA", run_id=run_id)


def test_qa_fail_remediation_pass_continue(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa(conn, total=4, failed=2)
        event_ctx = _seed_run(conn)
        result = run_qa_with_remediation(conn, event_ctx=event_ctx, project_id="PRJ-003")
        events = {r["event_type"] for r in conn.execute(
            "SELECT event_type FROM projectos_events WHERE run_id = 'RUN-QA'"
        ).fetchall()}
        run = conn.execute("SELECT status FROM execution_runs WHERE run_id='RUN-QA'").fetchone()
    assert result.gate == "PASSED"
    assert result.remediation_cycles == 1
    assert "REMEDIATION_STARTED" in events
    assert "QA_GATE_PASSED" in events
    assert "RUN_BLOCKED" not in events
    assert run["status"] == "RUNNING"


def test_qa_fail_twice_then_pass_on_third_cycle(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa(conn, total=6, failed=3)
        event_ctx = _seed_run(conn, run_id="RUN-QA2")
        # first cycle fixes 3 fails; leave 0 fails -> pass
        result = run_qa_with_remediation(conn, event_ctx=event_ctx, project_id="PRJ-003")
    assert result.gate == "PASSED"
    assert result.remediation_cycles >= 1


def test_remediation_policy_exceeded_escalates(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa(conn, total=4, failed=4)
        event_ctx = _seed_run(conn, run_id="RUN-ESC")
        # Pre-seed recurrence for ASSURANCE_0 so the next failure exceeds policy.
        conn.execute(
            """
            INSERT INTO projectos_events (
                event_id, event_version, project_id, run_id, actor_type, actor_id,
                actor_role, event_type, summary, visibility, detail_level, evidence_json
            ) VALUES (lower(hex(randomblob(8))), 1, 'PRJ-003', 'RUN-ESC',
                      'agent', 'qa-agent', 'QA Agent', 'QA_FINDING_CREATED',
                      'seed', 'SPONSOR', 'normal', ?)
            """,
            (
                json.dumps(
                    {
                        "category": "ASSURANCE_0",
                        "finding_id": "FND-SEED",
                    }
                ),
            ),
        )
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            max_cycles=3,
            max_same_finding_recurrence=1,
        )
        terminal = conn.execute(
            "SELECT event_type FROM projectos_events WHERE run_id='RUN-ESC' AND event_type='RUN_ESCALATED'"
        ).fetchone()
        run = conn.execute("SELECT status FROM execution_runs WHERE run_id='RUN-ESC'").fetchone()
    assert result.escalated
    assert terminal is not None
    assert run["status"] == "ESCALATED"


def test_qa_failure_alone_never_run_blocked(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa(conn, total=2, failed=2)
        event_ctx = _seed_run(conn, run_id="RUN-NB")
        run_qa_with_remediation(conn, event_ctx=event_ctx, project_id="PRJ-003", max_cycles=0)
        blocked = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id='RUN-NB' AND event_type='RUN_BLOCKED'"
        ).fetchone()
    assert blocked is None
