"""PRJ-007 architecture regression: Slack transport, release scope, run progression."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext, emit_projectos_event
from projectos.event_dispatcher import dispatch_event_outbox
from projectos.execution_run import create_execution_run
from projectos.migrate import initialize_database
from projectos.operator import (
    COMPONENT_SLACK,
    OperatorPaths,
    maybe_respawn_slack_adapter,
    operator_health,
    read_pid,
)
from projectos.release_gate_remediation import (
    ensure_release_readiness_remediation,
    is_correctable_release_block,
)
from projectos.release_readiness import ReleaseEvaluation, evaluate_release_job
from projectos.release_scope import resolve_release_scope
from projectos.run_next_actions import list_active_next_actions, reconcile_run_next_actions
from projectos.services.context import ServiceContext
from projectos.slack_ingress import process_slack_ingress_batch
from projectos.slack_socket import process_socket_envelope
from projectos.slack_state import write_slack_state
from projectos.store import (
    add_slack_interface_channel,
    create_job,
    get_job,
    mark_succeeded,
)


def _ctx(tmp_path: Path, *, project_id: str = "PRJ-007") -> ServiceContext:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, project_human_id=project_id, project_name="Seven")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": project_id, "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id="C007", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _fat_plan(*, stories: list[str]) -> dict:
    jobs = [
        {
            "human_id": "PRJ-007-ARCH-001",
            "queue": "ARCHITECTURE",
            "agent_role": "ARCHITECTURE",
            "priority": 10,
        },
    ]
    for idx, story in enumerate(stories, start=1):
        jobs.append(
            {
                "human_id": f"PRJ-007-DEL-{idx:03d}",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "work_item_type": "story",
                "work_item_human_id": story,
                "priority": 20 + idx,
            }
        )
    jobs.extend(
        [
            {
                "human_id": "PRJ-007-INT-001",
                "queue": "INTEGRATION",
                "agent_role": "INTEGRATION",
                "priority": 90,
            },
            {
                "human_id": "PRJ-007-REL-001",
                "queue": "RELEASE",
                "agent_role": "RELEASE",
                "priority": 100,
                "depends_on": ["PRJ-007-INT-001"],
            },
        ]
    )
    return {
        "schema_version": 1,
        "project_human_id": "PRJ-007",
        "iteration_human_id": "ITER-001",
        "sponsor_authority": "approved",
        "jobs": jobs,
    }


def test_socket_ack_returns_before_slow_intake_completes(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    started = threading.Event()
    finished = threading.Event()

    def slow_handler(*args, **kwargs):
        started.set()
        time.sleep(0.3)
        finished.set()
        return {"text": "created", "response_type": "in_channel", "_outbox_delivered": True}

    monkeypatch.setattr("projectos.slack_ingress.handle_events_api_payload", slow_handler)
    envelope = {
        "envelope_id": "env-slow",
        "type": "events_api",
        "payload": {
            "team_id": "T1",
            "event_id": "Ev-slow",
            "event": {
                "type": "message",
                "channel": "C007",
                "channel_type": "group",
                "ts": "200.0",
                "user": "U1",
                "text": "Start a new project to build a calculator.",
            },
        },
    }
    t0 = time.perf_counter()
    result = process_socket_envelope(ctx, envelope)
    ack_ms = (time.perf_counter() - t0) * 1000
    assert result["enqueued"] is True
    assert ack_ms < 200
    assert not finished.is_set()
    process_slack_ingress_batch(ctx)
    assert started.is_set()
    finished.wait(timeout=2)


def test_disconnect_projectos_continues_outbox_replays_once(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    posts: list[str] = []

    def fake_post(url, headers, body=None):
        if "chat.postMessage" in str(url):
            posts.append(str((body or {}).get("text") or ""))
            return {"ok": True}
        return {"ok": True}

    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-007",
                run_id="RUN-TEST",
                slack_channel_id="C007",
                slack_thread_ts="1.0",
            ),
            event_type="WORK_EXECUTION_AUTHORIZED",
            summary="Pending while disconnected",
            actor_id="pm-agent",
        )
        conn.commit()

    dispatch_event_outbox(ctx.db_path, http_post=fake_post)
    assert len(posts) == 1
    dispatch_event_outbox(ctx.db_path, http_post=fake_post)
    assert len(posts) == 1


def test_operator_respawns_dead_slack_adapter(tmp_path: Path, monkeypatch) -> None:
    paths = OperatorPaths(run_dir=tmp_path / "run", log_dir=tmp_path / "logs")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    (paths.run_dir / "slack_adapter.pid").write_text("999999", encoding="utf-8")
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.operator.load_operator_config",
        lambda *a, **k: type("C", (), {"slack_enabled": True, "slack_poll_seconds": 1})(),
    )
    monkeypatch.setattr("projectos.operator.spawn_logged", lambda *a, **k: __import__("projectos.operator", fromlist=["write_pid"]).write_pid(paths, COMPONENT_SLACK, 4242) or 4242)
    monkeypatch.setattr("projectos.operator.pid_is_alive", lambda pid: False)
    assert maybe_respawn_slack_adapter(ctx, paths=paths)
    assert read_pid(paths, COMPONENT_SLACK) == 4242


def _seed_accepted_plan(
    conn,
    *,
    repository_root: str,
    plan: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO pm_plan_runs (
            project_human_id, repository_root, iteration_human_id,
            dry_run, plan_json, status
        ) VALUES (?, ?, ?, 0, ?, 'accepted')
        """,
        (
            plan["project_human_id"],
            repository_root,
            plan.get("iteration_human_id"),
            json.dumps(plan),
        ),
    )


def test_release_scope_uses_plan_not_hardcoded_ids(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    plan = _fat_plan(stories=["US-101", "US-202", "US-303"])
    with connection(ctx.db_path) as conn:
        _seed_accepted_plan(
            conn,
            repository_root=str((tmp_path / "repo").resolve()),
            plan=plan,
        )
        release = create_job(
            conn,
            human_id="PRJ-007-REL-001",
            project_human_id="PRJ-007",
            repository_root=str(tmp_path / "repo"),
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            run_id="RUN-19708410",
        )
        scope = resolve_release_scope(conn, release)
        assert scope.delivery_story_ids == ["US-101", "US-202", "US-303"]
        assert "US-007" not in scope.delivery_story_ids
        assert "US-008" not in scope.delivery_story_ids


def test_correctable_release_blocked_schedules_pm_remediation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn,
            project_id="PRJ-007",
            handoff_id=None,
            request_type="WORK",
            objective="release remediation",
        )
        conn.execute(
            "UPDATE execution_runs SET status = 'RUNNING' WHERE run_id = ?",
            (run.run_id,),
        )
        release = create_job(
            conn,
            human_id="PRJ-007-REL-001",
            project_human_id="PRJ-007",
            repository_root=str(tmp_path / "repo"),
            agent_role="RELEASE",
            queue="RELEASE",
            status="BLOCKED",
            run_id=run.run_id,
        )
        reasons = ["no SUCCEEDED DELIVERY for US-101"]
        assert is_correctable_release_block(reasons)
        release_eval = ReleaseEvaluation(
            approved=False,
            reasons=reasons,
            candidate_sha="abc",
            evidence_dir=tmp_path / "ev",
            readiness_report_path=tmp_path / "ev" / "report.json",
            qa_package_path=None,
            release_human_id=None,
            release_status=None,
            iteration_status=None,
            workspace_clean=True,
            workspace_head="abc",
            outcome="GATE_REJECTED",
        )
        (tmp_path / "ev").mkdir(exist_ok=True)
        (tmp_path / "ev" / "report.json").write_text("{}", encoding="utf-8")
        action_id = ensure_release_readiness_remediation(
            conn,
            event_ctx=EventContext(project_id="PRJ-007", run_id=run.run_id),
            job=release,
            release_eval=release_eval,
            repository_root=str(tmp_path / "repo"),
        )
        assert action_id.startswith("RNA-")
        refreshed = get_job(conn, release.id)
        assert refreshed.status == "READY"


def test_completed_job_not_pending_next_action(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn,
            project_id="PRJ-007",
            handoff_id=None,
            request_type="WORK",
            objective="next action",
        )
        conn.execute(
            "UPDATE execution_runs SET status = 'RUNNING' WHERE run_id = ?",
            (run.run_id,),
        )
        arch = create_job(
            conn,
            human_id="PRJ-007-ARCH-001",
            project_human_id="PRJ-007",
            repository_root=str(tmp_path / "repo"),
            agent_role="ARCHITECTURE",
            queue="ARCHITECTURE",
            status="READY",
            run_id=run.run_id,
        )
        from projectos.run_next_actions import persist_run_next_action

        persist_run_next_action(
            conn,
            run_id=run.run_id,
            project_id="PRJ-007",
            action_type="EXECUTABLE_JOB",
            orchestration_job_id=arch.id,
        )
        mark_succeeded(conn, arch.id, output_ref=None, candidate_git_sha="sha1")
        reconcile_run_next_actions(conn, run_id=run.run_id, project_id="PRJ-007")
        live = list_active_next_actions(conn, run_id=run.run_id)
        assert all(
            int(a.get("orchestration_job_id") or 0) != arch.id for a in live
        )


def test_dashboard_health_distinguishes_connected_and_process_dead(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "slack_socket.json"
    monkeypatch.setattr("projectos.slack_state.STATE_PATH", state_path)
    write_slack_state({"status": "connected", "detail": "live"})
    paths = OperatorPaths(run_dir=tmp_path / "run")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    (paths.run_dir / "slack_adapter.pid").write_text("999999", encoding="utf-8")
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.operator.load_operator_config",
        lambda *a, **k: type(
            "C",
            (),
            {
                "slack_enabled": True,
                "daemon_enabled": False,
                "dashboard_enabled": False,
                "api_host": "127.0.0.1",
                "api_port": 1,
                "dashboard_host": "127.0.0.1",
                "dashboard_port": 1,
            },
        )(),
    )
    health = operator_health(ctx, paths=paths)
    slack = next(c for c in health["components"] if c["name"] == "slack_adapter")
    assert slack["status"] == "process_dead"


def test_prj007_closed_loop_release_with_arbitrary_stories(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    plan = _fat_plan(stories=["US-001", "US-002", "US-003"])
    with connection(ctx.db_path) as conn:
        _seed_accepted_plan(
            conn,
            repository_root=str((tmp_path / "repo").resolve()),
            plan=plan,
        )
        release = create_job(
            conn,
            human_id="PRJ-007-REL-001",
            project_human_id="PRJ-007",
            repository_root=str(tmp_path / "repo"),
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-001",
            run_id="RUN-19708410",
        )
        from projectos.store import set_job_source_provenance

        set_job_source_provenance(conn, release.id, source_delivery_job_id=None, source_candidate_sha="deadbeef")
        scope = resolve_release_scope(conn, release)
        assert scope.delivery_story_ids == ["US-001", "US-002", "US-003"]

        class _Ops:
            def resolve_db(self, repository_root: Path) -> Path:
                return tmp_path / "pc.db"

            def run(self, repository_root: Path, args: list[str], *, db_path: Path | None = None):
                return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        monkeypatch.setattr(
            "projectos.release_readiness.current_head_sha",
            lambda _w: "deadbeef",
        )
        monkeypatch.setattr("projectos.release_readiness.is_dirty", lambda _w: False)
        monkeypatch.setattr(
            "projectos.release_readiness.resolve_validated_repo",
            lambda *a, **k: type(
                "V",
                (),
                {
                    "git_root": str(tmp_path / "repo"),
                    "entry": type("E", (), {"project_human_id": "PRJ-007"})(),
                },
            )(),
        )
        ev = evaluate_release_job(
            conn,
            release,
            workspace=tmp_path / "repo",
            registry_path=ctx.registry_path,
            ops=_Ops(),
            expected_integration_sha="deadbeef",
        )
        joined = "; ".join(ev.reasons)
        assert "US-007" not in joined
        assert "US-008" not in joined


def test_release_remediation_retry_after_correctable_block(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn,
            project_id="PRJ-007",
            handoff_id=None,
            request_type="WORK",
            objective="release retry",
        )
        conn.execute(
            "UPDATE execution_runs SET status = 'RUNNING' WHERE run_id = ?",
            (run.run_id,),
        )
        release = create_job(
            conn,
            human_id="PRJ-007-REL-001",
            project_human_id="PRJ-007",
            repository_root=str(tmp_path / "repo"),
            agent_role="RELEASE",
            queue="RELEASE",
            status="BLOCKED",
            run_id=run.run_id,
        )
        release_eval = ReleaseEvaluation(
            approved=False,
            reasons=["no SUCCEEDED DELIVERY for US-001"],
            candidate_sha="abc",
            evidence_dir=tmp_path / "ev",
            readiness_report_path=tmp_path / "ev" / "report.json",
            qa_package_path=None,
            release_human_id=None,
            release_status=None,
            iteration_status=None,
            workspace_clean=True,
            workspace_head="abc",
            outcome="GATE_REJECTED",
        )
        (tmp_path / "ev").mkdir(exist_ok=True)
        (tmp_path / "ev" / "report.json").write_text("{}", encoding="utf-8")
        ensure_release_readiness_remediation(
            conn,
            event_ctx=EventContext(project_id="PRJ-007", run_id=run.run_id),
            job=release,
            release_eval=release_eval,
            repository_root=str(tmp_path / "repo"),
        )
        refreshed = get_job(conn, release.id)
        assert refreshed.status == "READY"


def test_restart_reconstructs_executable_work(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn,
            project_id="PRJ-007",
            handoff_id=None,
            request_type="WORK",
            objective="recovery",
        )
        conn.execute(
            "UPDATE execution_runs SET status = 'RUNNING' WHERE run_id = ?",
            (run.run_id,),
        )
        delivery = create_job(
            conn,
            human_id="PRJ-007-DEL-001",
            project_human_id="PRJ-007",
            repository_root=str(tmp_path / "repo"),
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            run_id=run.run_id,
        )
        from projectos.recover import run_recovery
        from projectos.run_next_actions import has_durable_next_action

        run_recovery(db_path=ctx.db_path, registry_path=ctx.registry_path)
        assert has_durable_next_action(
            conn, run_id=run.run_id, project_id="PRJ-007"
        )


def test_pending_ingress_survives_daemon_restart_once(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    envelope = {
        "envelope_id": "env-restart-once",
        "type": "events_api",
        "payload": {
            "team_id": "T1",
            "event_id": "Ev-restart",
            "event": {
                "type": "message",
                "channel": "C007",
                "channel_type": "group",
                "ts": "201.0",
                "user": "U1",
                "text": "/projectos help",
            },
        },
    }
    calls: list[int] = []

    def handler(*args, **kwargs):
        calls.append(1)
        return {"text": "help", "response_type": "ephemeral"}

    monkeypatch.setattr("projectos.slack_ingress.handle_events_api_payload", handler)
    process_socket_envelope(ctx, envelope)
    batch1 = process_slack_ingress_batch(ctx, claimed_by="daemon-a")
    assert batch1["processed"] == 1
    batch2 = process_slack_ingress_batch(ctx, claimed_by="daemon-b")
    assert batch2["processed"] == 0
    assert len(calls) == 1


def test_duplicate_new_project_envelope_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    from projectos.slack_socket import handle_events_api_payload

    ctx = _ctx(tmp_path)
    created: list[str] = []

    def fake_create(ctx_, request, **kwargs):
        created.append(request.dedup_key or "missing")
        from projectos.project_creation import ProjectCreationResult

        return ProjectCreationResult(
            project_human_id="PRJ-008",
            repository_root=tmp_path / "p8",
            handoff_id="HND-1",
            run_id="RUN-NEW",
            reply_text="New project initiated PRJ-008",
            idempotent_replay=False,
        )

    monkeypatch.setattr(
        "projectos.project_creation.create_project_from_sponsor_request",
        fake_create,
    )
    payload = {
        "team_id": "T1",
        "event_id": "Ev-dup",
        "event": {
            "type": "app_mention",
            "channel": "C007",
            "ts": "300.0",
            "user": "U1",
            "text": "<@UBOT> Start a new project to build a widget.",
        },
    }
    handle_events_api_payload(ctx, payload, bot_user_id="UBOT")
    handle_events_api_payload(ctx, payload, bot_user_id="UBOT")
    assert len(created) == 1


def test_slack_state_persists_reconnect_metadata(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "slack_socket.json"
    monkeypatch.setattr("projectos.slack_state.STATE_PATH", state_path)
    write_slack_state(
        {
            "status": "reconnecting",
            "reconnect_attempt": 3,
            "last_disconnect_reason": "code=1006 reason=",
        }
    )
    from projectos.slack_state import read_slack_state

    state = read_slack_state(state_path)
    assert state["reconnect_attempt"] == 3
    assert "1006" in str(state["last_disconnect_reason"])


def test_intentional_slack_stop_sets_shutdown_flag(tmp_path: Path, monkeypatch) -> None:
    from projectos.slack_adapter import (
        _SHUTDOWN_FLAG,
        clear_slack_adapter_shutdown,
        request_slack_adapter_shutdown,
    )

    monkeypatch.setattr("projectos.slack_adapter._SHUTDOWN_FLAG", tmp_path / "shutdown.flag")
    from projectos.slack_adapter import _SHUTDOWN_FLAG as flag

    clear_slack_adapter_shutdown()
    assert not flag.is_file()
    request_slack_adapter_shutdown()
    assert flag.is_file()
