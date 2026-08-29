"""Explicit test doubles for closed-loop orchestration tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

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


def _record_assurance_for_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    passed: bool,
    evidence_ref: str,
    assurance_job_ids: list[int] | None = None,
) -> None:
    rows = conn.execute(
        """
        SELECT assurance_job_id FROM qa_evidence
        WHERE candidate_git_sha = ? AND result = 'pending' AND assurance_job_id IS NOT NULL
        """,
        (candidate_id,),
    ).fetchall()
    targets = [int(row["assurance_job_id"]) for row in rows]
    if assurance_job_ids:
        allowed = set(assurance_job_ids)
        targets = [job_id for job_id in targets if job_id in allowed] or targets
    for job_id in targets:
        assurance = get_job(conn, job_id)
        if assurance is None:
            continue
        record_assurance_result(
            conn,
            assurance,
            passed=passed,
            evidence_ref=evidence_ref,
        )


class SequencedAssuranceExecutor:
    """Test-only assurance executor with explicit pass/fail sequence per call."""

    def __init__(self, outcomes: list[bool]) -> None:
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
        passed = self.outcomes[index]
        _record_assurance_for_candidate(
            conn,
            candidate_id=candidate_id,
            passed=passed,
            evidence_ref=f"fake-assurance:{candidate_id}:{index}",
            assurance_job_ids=assurance_job_ids or None,
        )


class FakeAssuranceExecutor:
    """Test-only assurance executor with explicit per-candidate outcomes."""

    def __init__(self, outcomes: dict[str, bool]) -> None:
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
        passed = self.outcomes.get(candidate_id)
        if passed is None:
            raise AssertionError(f"No scripted assurance outcome for candidate {candidate_id!r}")
        _record_assurance_for_candidate(
            conn,
            candidate_id=candidate_id,
            passed=passed,
            evidence_ref=f"fake-assurance:{candidate_id}",
            assurance_job_ids=assurance_job_ids or None,
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
