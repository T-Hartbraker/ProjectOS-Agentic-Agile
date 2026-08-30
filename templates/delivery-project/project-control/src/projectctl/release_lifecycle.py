"""Governed release status lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from projectctl.audit import row_to_dict, write_audit
from projectctl.gitutil import GitError, GitSnapshot, inspect_git, normalize_git_sha

RELEASE_STATUSES = frozenset(
    {
        "planned",
        "candidate",
        "qa_passed",
        "released",
        "superseded",
        "withdrawn",
    }
)

# Directed edges: from_status -> allowed to_status values
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"candidate", "withdrawn"}),
    "candidate": frozenset({"qa_passed", "withdrawn", "planned"}),
    "qa_passed": frozenset({"released", "withdrawn", "candidate"}),
    "released": frozenset({"superseded", "withdrawn"}),
    "superseded": frozenset(),
    "withdrawn": frozenset(),
}

BLOCKING_DEFECT_SEVERITIES = frozenset(
    {"critical", "high", "sev-1", "sev-2", "blocker", "p0"}
)


class ReleaseLifecycleError(Exception):
    """Invalid release transition or unmet gate."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _require_transition(current: str, target: str) -> None:
    if target not in RELEASE_STATUSES:
        raise ReleaseLifecycleError(
            f"Unknown release status: {target}. "
            f"Allowed: {', '.join(sorted(RELEASE_STATUSES))}"
        )
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ReleaseLifecycleError(
            f"Invalid transition: {current} -> {target}. "
            f"Allowed from {current}: {', '.join(sorted(allowed)) or '(none)'}"
        )


def _count_blocking_defects(conn: sqlite3.Connection, project_pk: int) -> int:
    rows = conn.execute(
        """
        SELECT severity FROM defects
        WHERE project_id = ? AND status = 'open'
        """,
        (project_pk,),
    ).fetchall()
    count = 0
    for row in rows:
        sev = str(row["severity"] or "").strip().lower()
        if sev in BLOCKING_DEFECT_SEVERITIES:
            count += 1
    return count


def _resolve_iteration_pk(
    conn: sqlite3.Connection, project_pk: int, iteration_human_id: str | None
) -> int | None:
    if not iteration_human_id:
        return None
    row = conn.execute(
        "SELECT id, project_id FROM iterations WHERE human_id = ?",
        (iteration_human_id,),
    ).fetchone()
    if row is None:
        raise ReleaseLifecycleError(f"Iteration not found: {iteration_human_id}")
    if int(row["project_id"]) != int(project_pk):
        raise ReleaseLifecycleError(
            f"Iteration {iteration_human_id} does not belong to this project"
        )
    return int(row["id"])


def _validate_dirty_tree_exception(
    conn: sqlite3.Connection, project_pk: int, decision_human_id: str
) -> str:
    row = conn.execute(
        "SELECT * FROM decisions WHERE human_id = ?",
        (decision_human_id,),
    ).fetchone()
    if row is None:
        raise ReleaseLifecycleError(
            f"Dirty-tree exception decision not found: {decision_human_id}"
        )
    if int(row["project_id"]) != int(project_pk):
        raise ReleaseLifecycleError(
            f"Exception decision {decision_human_id} is not for this project"
        )
    status = str(row["status"] or "").lower()
    if status not in {"accepted", "approved", "open"}:
        raise ReleaseLifecycleError(
            f"Exception decision {decision_human_id} status must be accepted/approved "
            f"(got {row['status']!r})"
        )
    return decision_human_id


def _sha_matches_head(sha: str, head: str | None) -> bool:
    if not head:
        return False
    return head == sha or head.startswith(sha) or sha.startswith(head)


def _gate_candidate(
    conn: sqlite3.Connection,
    release: sqlite3.Row,
    *,
    git_sha: str | None,
    dirty_tree_exception: str | None,
    git_snapshot: GitSnapshot,
) -> dict[str, Any]:
    if not git_sha:
        raise ReleaseLifecycleError(
            "Transition to candidate requires --git-sha <commit>"
        )
    try:
        sha = normalize_git_sha(git_sha)
    except GitError as exc:
        raise ReleaseLifecycleError(str(exc)) from exc

    if not git_snapshot.sha_exists(sha):
        raise ReleaseLifecycleError(
            f"Git revision is not identifiable in this repository: {sha}"
        )

    needs_exception = False
    reasons: list[str] = []
    if not git_snapshot.working_tree_clean:
        needs_exception = True
        reasons.append("working tree is dirty")
    if not _sha_matches_head(sha, git_snapshot.head_sha):
        needs_exception = True
        reasons.append(
            f"git SHA {sha} does not match HEAD {git_snapshot.head_sha}"
        )

    exception_id: str | None = None
    if needs_exception:
        if not dirty_tree_exception:
            raise ReleaseLifecycleError(
                "Candidate approval blocked ("
                + "; ".join(reasons)
                + "). Provide a formally recorded approved exception via "
                "--dirty-tree-exception DEC-xxx, or commit/checkout a clean "
                "tree at the candidate revision."
            )
        exception_id = _validate_dirty_tree_exception(
            conn, int(release["project_id"]), dirty_tree_exception
        )
    elif dirty_tree_exception:
        exception_id = _validate_dirty_tree_exception(
            conn, int(release["project_id"]), dirty_tree_exception
        )

    return {
        "git_sha": sha,
        "dirty_tree_exception": exception_id,
    }


def _gate_qa_passed(
    *,
    qa_evidence_ref: str | None,
    existing_qa_ref: str | None,
) -> dict[str, Any]:
    ref = qa_evidence_ref or existing_qa_ref
    if not ref:
        raise ReleaseLifecycleError(
            "Transition to qa_passed requires QA evidence "
            "(--qa-evidence path to independent QA recommendation)."
        )
    path = Path(ref)
    if not path.is_file():
        raise ReleaseLifecycleError(f"QA evidence file not found: {ref}")
    return {"qa_evidence_ref": str(path)}


def _gate_released(conn: sqlite3.Connection, release: Any) -> None:
    """release may be sqlite3.Row or a merged dict."""
    git_sha = release["git_sha"]
    qa_evidence_ref = release["qa_evidence_ref"]
    artifact = release["artifact_ref"]
    project_id = int(release["project_id"])

    if not git_sha:
        raise ReleaseLifecycleError(
            "Cannot release: git commit SHA is not recorded on the release"
        )
    if not qa_evidence_ref:
        raise ReleaseLifecycleError(
            "Cannot release: QA evidence reference is missing"
        )
    qa_path = Path(str(qa_evidence_ref))
    if not qa_path.is_file():
        raise ReleaseLifecycleError(
            f"Cannot release: QA evidence missing on disk: {qa_path}"
        )

    if not artifact:
        raise ReleaseLifecycleError(
            "Cannot release: artifact_ref is required "
            "(package path with release notes/checksums)"
        )
    art_path = Path(str(artifact))
    if art_path.is_dir():
        notes = art_path / "release-notes.md"
        checksums = art_path / "checksums.txt"
        if not notes.is_file() or not checksums.is_file():
            raise ReleaseLifecycleError(
                f"Cannot release: package incomplete under {art_path} "
                "(need release-notes.md and checksums.txt)"
            )
    elif not art_path.is_file():
        raise ReleaseLifecycleError(
            f"Cannot release: artifact_ref not found: {artifact}"
        )

    blocking = _count_blocking_defects(conn, project_id)
    if blocking:
        raise ReleaseLifecycleError(
            f"Cannot release: {blocking} open blocking defect(s) remain"
        )


def apply_release_transition(
    conn: sqlite3.Connection,
    human_id: str,
    target_status: str,
    *,
    git_sha: str | None = None,
    dirty_tree_exception: str | None = None,
    artifact_ref: str | None = None,
    qa_evidence_ref: str | None = None,
    iteration_id: str | None = None,
    reason: str | None = None,
    git_snapshot: GitSnapshot | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and apply a release status transition with audit logging."""
    release = conn.execute(
        "SELECT * FROM releases WHERE human_id = ?", (human_id,)
    ).fetchone()
    if release is None:
        raise ReleaseLifecycleError(f"Release not found: {human_id}")

    current = str(release["status"])
    target = target_status.strip().lower()
    _require_transition(current, target)

    updates: dict[str, Any] = {
        "status": target,
        "updated_at": _utcnow(),
    }
    released_at: str | None = release["released_at"]

    snapshot = git_snapshot
    if target == "candidate":
        if snapshot is None:
            try:
                snapshot = inspect_git(repo_root)
            except GitError as exc:
                raise ReleaseLifecycleError(str(exc)) from exc
        cand = _gate_candidate(
            conn,
            release,
            git_sha=git_sha or release["git_sha"],
            dirty_tree_exception=dirty_tree_exception,
            git_snapshot=snapshot,
        )
        updates["git_sha"] = cand["git_sha"]
        if cand["dirty_tree_exception"] is not None:
            updates["dirty_tree_exception"] = cand["dirty_tree_exception"]
        released_at = None
    elif target == "qa_passed":
        qa = _gate_qa_passed(
            qa_evidence_ref=qa_evidence_ref,
            existing_qa_ref=release["qa_evidence_ref"],
        )
        updates["qa_evidence_ref"] = qa["qa_evidence_ref"]
        if not release["git_sha"] and not git_sha:
            raise ReleaseLifecycleError(
                "Cannot enter qa_passed without a recorded git SHA "
                "(promote to candidate first)"
            )
        if git_sha:
            updates["git_sha"] = normalize_git_sha(git_sha)
        released_at = None
    elif target == "released":
        if artifact_ref is not None:
            updates["artifact_ref"] = artifact_ref
        merged = {key: release[key] for key in release.keys()}
        merged.update(updates)
        _gate_released(conn, merged)
        updates["released_at"] = _utcnow()

    if target in {"planned", "candidate", "qa_passed"}:
        updates["released_at"] = None

    if artifact_ref is not None and "artifact_ref" not in updates:
        updates["artifact_ref"] = artifact_ref

    if iteration_id is not None:
        updates["iteration_id"] = _resolve_iteration_pk(
            conn, int(release["project_id"]), iteration_id
        )

    before = row_to_dict(release)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [human_id]
    conn.execute(
        f"UPDATE releases SET {set_clause} WHERE human_id = ?",
        values,
    )
    after_row = conn.execute(
        "SELECT * FROM releases WHERE human_id = ?", (human_id,)
    ).fetchone()
    after = row_to_dict(after_row)

    write_audit(
        conn,
        action=f"release.status.{current}->{target}",
        entity_type="release",
        entity_id=human_id,
        before_state=before,
        after_state=after,
        reason=reason or f"release transition {current} -> {target}",
    )
    return after  # type: ignore[return-value]


def complete_release(
    conn: sqlite3.Connection,
    human_id: str,
    *,
    artifact_ref: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """planned/candidate bypass is rejected: only qa_passed -> released."""
    release = conn.execute(
        "SELECT * FROM releases WHERE human_id = ?", (human_id,)
    ).fetchone()
    if release is None:
        raise ReleaseLifecycleError(f"Release not found: {human_id}")
    if release["status"] != "qa_passed":
        raise ReleaseLifecycleError(
            f"release complete rejected: status is {release['status']!r}, "
            "expected 'qa_passed'. Promote via candidate and qa_passed first."
        )
    return apply_release_transition(
        conn,
        human_id,
        "released",
        artifact_ref=artifact_ref,
        reason=reason or "release complete",
    )
