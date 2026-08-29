"""Controlled candidate integration (no self-merge to release by delivery)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from projectos.db import connection
from projectos.errors import GitRepositoryError, OrchestrationError
from projectos.gitutil import resolve_git_root
from projectos.store import insert_integration_run, utc_now_iso


@dataclass(frozen=True)
class IntegrationResult:
    status: str
    integrated_sha: str | None
    conflict_paths: list[str]
    error: str | None = None


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise GitRepositoryError(err or f"git {' '.join(args)} failed")
    return completed.stdout


def integrate_candidates(
    *,
    repository_root: Path,
    project_human_id: str,
    source_shas: list[str],
    source_job_ids: list[int],
    iteration_human_id: str | None = None,
    integration_branch: str = "projectos/integration",
    db_path: Path | str | None = None,
) -> IntegrationResult:
    """Merge selected candidate SHAs into a clean integration branch."""
    root = resolve_git_root(repository_root)
    if not source_shas:
        raise OrchestrationError("No source SHAs to integrate")

    # Verify SHAs exist
    for sha in source_shas:
        try:
            _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=root)
        except GitRepositoryError as exc:
            raise OrchestrationError(f"Unknown candidate SHA {sha}: {exc}") from exc

    now = utc_now_iso()
    run_id = None
    if db_path is not None:
        with connection(db_path) as conn:
            run_id = insert_integration_run(
                conn,
                project_human_id=project_human_id,
                repository_root=root,
                iteration_human_id=iteration_human_id,
                source_job_ids=source_job_ids,
                source_shas=source_shas,
                status="integrating",
                updated_at=now,
            )

    try:
        # Ensure integration branch from first parent HEAD of main/master/current
        try:
            _git(["rev-parse", "--verify", integration_branch], cwd=root)
            _git(["checkout", integration_branch], cwd=root)
        except GitRepositoryError:
            _git(["checkout", "-b", integration_branch], cwd=root)

        conflicts: list[str] = []
        for sha in source_shas:
            completed = subprocess.run(
                ["git", "merge", "--no-ff", "--no-commit", sha],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                status = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                paths = [p for p in (status.stdout or "").splitlines() if p.strip()]
                conflicts.extend(paths)
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=root,
                    check=False,
                    capture_output=True,
                )
                # Do not silently resolve semantic conflicts.
                result = IntegrationResult(
                    status="conflict",
                    integrated_sha=None,
                    conflict_paths=sorted(set(conflicts)),
                    error="Merge conflict during integration",
                )
                if db_path is not None and run_id is not None:
                    with connection(db_path) as conn:
                        conn.execute(
                            """
                            UPDATE integration_runs
                            SET status='conflict', conflict_paths_json=?, error=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                json.dumps(result.conflict_paths),
                                result.error,
                                utc_now_iso(),
                                run_id,
                            ),
                        )
                return result
            # Commit each successful merge for provenance.
            _git(
                [
                    "commit",
                    "--allow-empty",
                    "-m",
                    f"projectos integrate {sha}",
                ],
                cwd=root,
            )

        integrated = _git(["rev-parse", "HEAD"], cwd=root).strip()
        result = IntegrationResult(
            status="succeeded",
            integrated_sha=integrated,
            conflict_paths=[],
        )
        if db_path is not None and run_id is not None:
            with connection(db_path) as conn:
                conn.execute(
                    """
                    UPDATE integration_runs
                    SET status='succeeded', integrated_sha=?, updated_at=?
                    WHERE id=?
                    """,
                    (integrated, utc_now_iso(), run_id),
                )
        return result
    except Exception as exc:  # noqa: BLE001
        if db_path is not None and run_id is not None:
            with connection(db_path) as conn:
                conn.execute(
                    """
                    UPDATE integration_runs
                    SET status='failed', error=?, updated_at=?
                    WHERE id=?
                    """,
                    (str(exc), utc_now_iso(), run_id),
                )
        return IntegrationResult(
            status="failed",
            integrated_sha=None,
            conflict_paths=[],
            error=str(exc),
        )
