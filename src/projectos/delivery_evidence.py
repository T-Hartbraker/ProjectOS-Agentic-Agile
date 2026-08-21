"""Delivery candidate evidence evaluation (exit 0 is not success)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from projectos.store import OrchestrationJob

_NO_CHANGE_RE = re.compile(r"(?im)^\s*OUTCOME:\s*NO_CHANGE\b")


@dataclass(frozen=True)
class CandidateEvaluation:
    ok: bool
    outcome: str | None  # None | NO_CHANGE
    error: str | None = None
    handoff_eligible: bool = False


def stdout_declares_no_change(stdout: str | None) -> bool:
    return bool(stdout and _NO_CHANGE_RE.search(stdout))


def evaluate_delivery_candidate(
    job: OrchestrationJob,
    *,
    base_git_sha: str | None,
    candidate_git_sha: str | None,
    dirty: bool | None,
    cursor_stdout: str | None = None,
    code_changing: bool = True,
) -> CandidateEvaluation:
    """Decide whether a DELIVERY Cursor run produced a valid candidate."""
    if not base_git_sha:
        return CandidateEvaluation(
            ok=False,
            outcome=None,
            error="DELIVERY requires a known base_git_sha",
        )
    if not candidate_git_sha:
        return CandidateEvaluation(
            ok=False,
            outcome=None,
            error="DELIVERY requires an identifiable candidate_git_sha",
        )
    if dirty:
        return CandidateEvaluation(
            ok=False,
            outcome=None,
            error=(
                "DELIVERY left uncommitted changes; dirty worktree cannot be "
                "marked SUCCEEDED"
            ),
        )

    no_change = stdout_declares_no_change(cursor_stdout) or bool(
        job.allows_no_change
    )
    if candidate_git_sha == base_git_sha:
        if no_change and code_changing:
            return CandidateEvaluation(
                ok=True,
                outcome="NO_CHANGE",
                handoff_eligible=False,
            )
        if not code_changing:
            return CandidateEvaluation(
                ok=True,
                outcome=None,
                handoff_eligible=False,
            )
        return CandidateEvaluation(
            ok=False,
            outcome=None,
            error=(
                "DELIVERY produced no candidate revision "
                f"(candidate_git_sha == base_git_sha == {base_git_sha}); "
                "Cursor exit 0 is not delivery success. Declare OUTCOME: NO_CHANGE "
                "with rationale only when no code change is legitimate."
            ),
        )

    return CandidateEvaluation(
        ok=True,
        outcome=None,
        handoff_eligible=True,
    )


def is_valid_qa_candidate(job: OrchestrationJob) -> bool:
    """True when a SUCCEEDED delivery may create or feed assurance."""
    if job.queue != "DELIVERY" or job.status != "SUCCEEDED":
        return False
    if job.outcome in {"INVALIDATED", "SUPERSEDED", "NO_CHANGE"}:
        return False
    if not job.candidate_git_sha or not job.base_git_sha:
        return False
    if job.candidate_git_sha == job.base_git_sha:
        return False
    return True
