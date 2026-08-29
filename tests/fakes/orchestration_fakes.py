"""Explicit test doubles for closed-loop orchestration tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from projectos.assurance_verdict import VERDICT_FAIL, VERDICT_INCONCLUSIVE, VERDICT_PASS
from projectos.candidate_model import CANDIDATE_TYPE_GIT_SHA
from projectos.domain_events import EventContext
from projectos.qa_handoff import record_assurance_result
from projectos.remediation_executor import RemediationExecutionResult
from projectos.remediation_store import RemediationWorkRecord
from projectos.store import get_job, mark_succeeded


def git_head_sha(repository_root: str | Path) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def git_commit_file(repository_root: str | Path, relative_path: str, content: str) -> str:
    repo = Path(repository_root)
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(["git", "add", relative_path], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            f"test commit {relative_path}",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    return git_head_sha(repo)


def _normalize_verdict(value: bool | str) -> str:
    if isinstance(value, bool):
        return VERDICT_PASS if value else VERDICT_FAIL
    normalized = str(value).upper()
    if normalized not in {VERDICT_PASS, VERDICT_FAIL, VERDICT_INCONCLUSIVE}:
        raise ValueError(f"Invalid assurance verdict {value!r}")
    return normalized


def _record_assurance_for_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    verdict: bool | str,
    evidence_ref: str,
    assurance_job_ids: list[int] | None = None,
    findings: list[dict] | None = None,
) -> None:
    rows = conn.execute(
        """
        SELECT e.assurance_job_id FROM qa_evidence e
        JOIN orchestration_jobs j ON j.id = e.assurance_job_id
        WHERE e.candidate_git_sha = ? AND e.result = 'pending'
          AND j.queue IN ('ASSURANCE_FUNCTIONAL', 'ASSURANCE_INTEGRATION', 'ASSURANCE_SECURITY', 'ASSURANCE_QUALITY')
        """,
        (candidate_id,),
    ).fetchall()
    targets = [int(row["assurance_job_id"]) for row in rows]
    if assurance_job_ids:
        allowed = set(assurance_job_ids)
        targets = [job_id for job_id in targets if job_id in allowed] or targets
    normalized = _normalize_verdict(verdict)
    for job_id in targets:
        assurance = get_job(conn, job_id)
        if assurance is None:
            continue
        record_assurance_result(
            conn,
            assurance,
            verdict=normalized,
            evidence_ref=evidence_ref,
            findings=findings,
        )
    mgr_row = conn.execute(
        """
        SELECT j.id FROM orchestration_jobs j
        JOIN qa_evidence e ON e.assurance_job_id = j.id
        WHERE e.candidate_git_sha = ? AND j.queue = 'QA_MANAGER' AND e.result = 'pending'
        """,
        (candidate_id,),
    ).fetchone()
    if mgr_row is not None:
        from projectos.qa_manager import execute_qa_manager_aggregation

        mgr_job = get_job(conn, int(mgr_row["id"]))
        if mgr_job is not None:
            try:
                execute_qa_manager_aggregation(conn, mgr_job)
            except Exception:
                pass


class SequencedAssuranceExecutor:
    """Test-only assurance executor with explicit verdict sequence per call."""

    def __init__(self, outcomes: list[bool | str]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        repository_root: str,
        candidate_id: str,
        candidate_type: str,
        run_id: str | None,
        remediation_cycle: int,
        assurance_job_ids: list[int],
    ) -> None:
        self.calls.append(candidate_id)
        index = len(self.calls) - 1
        if index >= len(self.outcomes):
            raise AssertionError(f"No scripted assurance outcome for call {index + 1}")
        verdict = self.outcomes[index]
        findings = None
        if _normalize_verdict(verdict) == VERDICT_FAIL:
            findings = [
                {
                    "finding_id": f"FND-TEST-{index + 1}",
                    "category": "SOURCE_CODE_DEFECT",
                    "severity": "high",
                    "evidence": f"test failure on {candidate_id}",
                    "affected_component": "test",
                    "expected_condition": "pass",
                    "actual_condition": "fail",
                    "recommended_owner_role": "SOURCE_CODE_DEFECT",
                    "retryable": True,
                }
            ]
        _record_assurance_for_candidate(
            conn,
            candidate_id=candidate_id,
            verdict=verdict,
            evidence_ref=f"fake-assurance:{candidate_id}:{index}",
            assurance_job_ids=assurance_job_ids or None,
            findings=findings,
        )


class FakeAssuranceExecutor:
    """Test-only assurance executor with explicit per-candidate outcomes."""

    def __init__(self, outcomes: dict[str, bool | str]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        repository_root: str,
        candidate_id: str,
        candidate_type: str,
        run_id: str | None,
        remediation_cycle: int,
        assurance_job_ids: list[int],
    ) -> None:
        self.calls.append(candidate_id)
        verdict = self.outcomes.get(candidate_id)
        if verdict is None:
            raise AssertionError(f"No scripted assurance outcome for candidate {candidate_id!r}")
        findings = None
        if _normalize_verdict(verdict) == VERDICT_FAIL:
            findings = [
                {
                    "finding_id": f"FND-TEST-{candidate_id[:8]}",
                    "category": "SOURCE_CODE_DEFECT",
                    "severity": "high",
                    "evidence": f"test failure on {candidate_id}",
                    "affected_component": "test",
                    "expected_condition": "pass",
                    "actual_condition": "fail",
                    "recommended_owner_role": "SOURCE_CODE_DEFECT",
                    "retryable": True,
                }
            ]
        _record_assurance_for_candidate(
            conn,
            candidate_id=candidate_id,
            verdict=verdict,
            evidence_ref=f"fake-assurance:{candidate_id}",
            assurance_job_ids=assurance_job_ids or None,
            findings=findings,
        )


def make_git_remediation_worker(repository_root: str | Path):
    """Test-only remediation worker that creates real git commits as candidates."""

    counter = {"n": 0}

    def _worker(
        conn: sqlite3.Connection,
        *,
        work: RemediationWorkRecord,
        event_ctx: EventContext,
        project_id: str,
        repository_root: str,
    ) -> RemediationExecutionResult:
        counter["n"] += 1
        sha = git_commit_file(
            repository_root,
            f"remediation/cycle-{work.remediation_cycle}-{counter['n']}.txt",
            f"fix cycle {work.remediation_cycle}\n",
        )
        if work.orchestration_job_id is not None:
            mark_succeeded(
                conn,
                work.orchestration_job_id,
                output_ref="fake-remediation",
                candidate_git_sha=sha,
            )
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="COMPLETED",
            target_candidate_id=sha,
            candidate_type=CANDIDATE_TYPE_GIT_SHA,
            evidence={"candidate_git_sha": sha, "candidate_type": CANDIDATE_TYPE_GIT_SHA},
        )

    return _worker
