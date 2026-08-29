"""QA evidence immutability — historical assessment results are append-only."""

from __future__ import annotations

import sqlite3

from projectos.errors import OrchestrationError

TERMINAL_QA_RESULTS = frozenset({"pass", "fail", "stale_rejected"})


class QAEvidenceImmutableError(OrchestrationError):
    """Raised when code attempts to rewrite historical QA assessment."""


def update_qa_evidence_result(
    conn: sqlite3.Connection,
    *,
    assurance_job_id: int,
    candidate_git_sha: str,
    new_result: str,
    evidence_ref: str | None = None,
    defect_human_id: str | None = None,
) -> None:
    """Set QA evidence result only from pending (initial evaluation) or stale transition."""
    row = conn.execute(
        """
        SELECT id, result FROM qa_evidence
        WHERE assurance_job_id = ? AND candidate_git_sha = ?
        """,
        (assurance_job_id, candidate_git_sha),
    ).fetchone()
    if row is None:
        raise OrchestrationError(
            f"qa_evidence missing for assurance_job_id={assurance_job_id} "
            f"candidate={candidate_git_sha}"
        )
    current = str(row["result"] or "")
    if current in TERMINAL_QA_RESULTS and current != new_result:
        raise QAEvidenceImmutableError(
            f"qa_evidence id={row['id']} result={current!r} is immutable; "
            f"cannot change to {new_result!r}. Create new evidence for a new candidate."
        )
    if current == "fail" and new_result == "pass":
        raise QAEvidenceImmutableError(
            "QA evidence FAIL cannot be mutated to PASS; remediation must produce a new candidate."
        )
    fields = ["result = ?"]
    values: list[object] = [new_result]
    if evidence_ref is not None:
        fields.append("evidence_ref = ?")
        values.append(evidence_ref)
    if defect_human_id is not None:
        fields.append("defect_human_id = ?")
        values.append(defect_human_id)
    values.extend([assurance_job_id, candidate_git_sha])
    conn.execute(
        f"""
        UPDATE qa_evidence
        SET {', '.join(fields)}
        WHERE assurance_job_id = ? AND candidate_git_sha = ?
        """,
        values,
    )


def assert_qa_evidence_not_remediated(conn: sqlite3.Connection, evidence_id: int) -> None:
    """Guard helper for tests and services."""
    row = conn.execute(
        "SELECT result FROM qa_evidence WHERE id = ?", (evidence_id,)
    ).fetchone()
    if row is None:
        raise OrchestrationError(f"qa_evidence id={evidence_id} not found")
    if str(row["result"]) == "fail":
        updated = conn.execute(
            "SELECT result FROM qa_evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if str(updated["result"]) == "pass":
            raise QAEvidenceImmutableError(
                f"qa_evidence id={evidence_id} was illegally mutated from fail to pass"
            )
