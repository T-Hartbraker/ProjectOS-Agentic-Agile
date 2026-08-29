"""Read-only quality/defect snapshot. Independent assurance; no QA pass writes."""

from __future__ import annotations

from typing import Any

from projectos.constants import ASSURANCE_QUEUES
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.store import OrchestrationJob, public_artifact_ref

_ACTIVE_STATUSES = frozenset({"QUEUED", "READY", "LEASED", "RUNNING", "RETRY_WAIT"})
_PASS_RESULTS = frozenset({"pass", "passed"})
_FAIL_RESULTS = frozenset({"fail"})
_UNREPORTED = "Not reported"


def _is_stale(result: str) -> bool:
    return "stale" in result.lower()


def _is_pass(result: str) -> bool:
    return result in _PASS_RESULTS


def _is_fail(result: str) -> bool:
    return result in _FAIL_RESULTS


def _defect_status(result: str | None) -> str:
    text = str(result or "")
    if _is_stale(text):
        return "stale"
    if _is_fail(text):
        return "open"
    if _is_pass(text):
        return "closed"
    return "recorded"


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _latest_finding(evidence: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    for row in evidence:
        if row["assurance_role"] == role:
            return {
                "role": role,
                "result": row["result"],
                "candidate_git_sha": row.get("candidate_git_sha"),
                "evidence_ref": public_artifact_ref(row.get("evidence_ref")),
                "job_human_id": row.get("assurance_job_human_id"),
            }
    return None


def _release_blocking_reasons(
    *,
    role_results: dict[str, str],
    evidence: list[dict[str, Any]],
    jobs: list[OrchestrationJob],
    defects: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    shas = [str(row["candidate_git_sha"]) for row in evidence if row.get("candidate_git_sha")]
    sha_note = shas[0] if shas else "none"
    for role in REQUIRED_ASSURANCE:
        result = role_results.get(role, "missing")
        if not _is_pass(result):
            reasons.append(f"{role} is {result} for candidate {sha_note}")
    open_defects = [item for item in defects if item["status"] == "open"]
    if open_defects:
        reasons.append(f"{len(open_defects)} open defect(s) block release")
    stale = sum(1 for row in evidence if _is_stale(str(row["result"])))
    if stale:
        reasons.append(f"{stale} stale QA evidence row(s) cannot approve the current candidate")
    open_assurance = [
        job
        for job in jobs
        if job.queue in ASSURANCE_QUEUES and job.status in _ACTIVE_STATUSES
    ]
    if open_assurance:
        reasons.append(f"{len(open_assurance)} open assurance job(s) have not finished")
    release_jobs = [job for job in jobs if job.queue == "RELEASE"]
    latest_release = release_jobs[-1] if release_jobs else None
    if latest_release is not None:
        outcome = latest_release.outcome or latest_release.status
        if outcome in {"GATE_REJECTED", "REJECTED"} or latest_release.status in {
            "FAILED",
            "BLOCKED",
        }:
            reasons.append(
                f"release job {latest_release.human_id} is {latest_release.status}"
                + (f" ({latest_release.outcome})" if latest_release.outcome else "")
            )
            if latest_release.last_error:
                reasons.append(latest_release.last_error)
    elif not evidence:
        reasons.append("no QA evidence recorded for this project")
    return reasons


def _lineage(
    jobs: list[OrchestrationJob],
    invalidations: list[dict[str, Any]],
    edges: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    by_id = {job.human_id: job for job in jobs}
    items: list[dict[str, Any]] = []
    for item in invalidations:
        items.append(
            {
                "kind": "invalidation",
                "delivery_job_human_id": item.get("delivery_job_human_id"),
                "assurance_job_human_id": None,
                "rework_job_human_id": item.get("rework_job_human_id"),
                "retest_job_human_id": None,
                "candidate_git_sha": None,
                "invalidated_candidate_sha": item.get("invalidated_candidate_sha"),
                "reason": item.get("reason"),
                "status": None,
            }
        )
    dependents: dict[str, list[str]] = {}
    depends_on: dict[str, list[str]] = {}
    for job_id, dep in edges:
        depends_on.setdefault(job_id, []).append(dep)
        dependents.setdefault(dep, []).append(job_id)
    for job in jobs:
        if job.queue != "DELIVERY" or not str(job.human_id).endswith("__REWORK"):
            continue
        assurance_ids = [
            dep
            for dep in depends_on.get(job.human_id, [])
            if by_id.get(dep) is not None and by_id[dep].queue in ASSURANCE_QUEUES
        ]
        retests = [
            hid
            for hid in dependents.get(job.human_id, [])
            if by_id.get(hid) is not None and by_id[hid].queue in ASSURANCE_QUEUES
        ]
        items.append(
            {
                "kind": "rework",
                "delivery_job_human_id": job.human_id,
                "assurance_job_human_id": assurance_ids[0] if assurance_ids else None,
                "rework_job_human_id": job.human_id,
                "retest_job_human_id": None,
                "candidate_git_sha": job.candidate_git_sha,
                "invalidated_candidate_sha": None,
                "reason": "QA failure rework",
                "status": job.status,
            }
        )
        for retest_id in retests:
            retest = by_id[retest_id]
            items.append(
                {
                    "kind": "retest",
                    "delivery_job_human_id": job.human_id,
                    "assurance_job_human_id": assurance_ids[0] if assurance_ids else None,
                    "rework_job_human_id": job.human_id,
                    "retest_job_human_id": retest.human_id,
                    "candidate_git_sha": retest.source_candidate_sha
                    or retest.candidate_git_sha,
                    "invalidated_candidate_sha": None,
                    "reason": "retest after rework",
                    "status": retest.status,
                }
            )
    return items


def build_quality_snapshot(
    *,
    project_human_id: str,
    jobs: list[OrchestrationJob],
    evidence: list[dict[str, Any]],
    invalidations: list[dict[str, Any]],
    edges: list[tuple[str, str]],
) -> dict[str, Any]:
    required = list(REQUIRED_ASSURANCE)
    pending = [row for row in evidence if row["result"] == "pending"]
    passed = [row for row in evidence if _is_pass(str(row["result"]))]
    failed = [row for row in evidence if _is_fail(str(row["result"]))]
    stale = [row for row in evidence if _is_stale(str(row["result"]))]
    role_results = {role: "missing" for role in required}
    for row in reversed(evidence):
        role = str(row["assurance_role"])
        if role in role_results:
            role_results[role] = str(row["result"])
    shas: list[str] = []
    seen_shas: set[str] = set()
    for row in evidence:
        sha = row.get("candidate_git_sha")
        if sha and sha not in seen_shas:
            seen_shas.add(str(sha))
            shas.append(str(sha))
    evidence_items = [
        {
            "assurance_role": row["assurance_role"],
            "result": row["result"],
            "candidate_git_sha": row.get("candidate_git_sha"),
            "evidence_ref": public_artifact_ref(row.get("evidence_ref")),
            "delivery_job_human_id": row.get("delivery_job_human_id"),
            "assurance_job_human_id": row.get("assurance_job_human_id"),
            "assurance_job_status": row.get("assurance_job_status"),
            "defect_human_id": row.get("defect_human_id"),
            "created_at": row.get("created_at"),
        }
        for row in evidence
    ]
    defects: list[dict[str, Any]] = []
    seen_defects: dict[str, dict[str, Any]] = {}
    for row in evidence:
        hid = row.get("defect_human_id")
        if not hid:
            continue
        seen_defects[str(hid)] = {
            "defect_human_id": str(hid),
            "severity": _UNREPORTED,
            "priority": _UNREPORTED,
            "status": _defect_status(str(row.get("result"))),
            "assurance_role": row.get("assurance_role"),
            "delivery_job_human_id": row.get("delivery_job_human_id"),
            "candidate_git_sha": row.get("candidate_git_sha"),
            "result": row.get("result"),
        }
    defects = list(seen_defects.values())
    by_severity: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for item in defects:
        _increment(by_severity, str(item["severity"]))
        _increment(by_priority, str(item["priority"]))
        _increment(by_status, str(item["status"]))
    return {
        "project_human_id": project_human_id,
        "qa_pass_authority": "independent_assurance_only",
        "developer_can_mark_qa_passed": False,
        "summary": {
            "required_roles": required,
            "role_results": role_results,
            "pending_count": len(pending),
            "passed_count": len(passed),
            "failed_count": len(failed),
            "stale_count": len(stale),
            "open_assurance_jobs": sum(
                1 for job in jobs if job.queue in ASSURANCE_QUEUES and job.status in _ACTIVE_STATUSES
            ),
            "evaluated_candidate_shas": shas,
        },
        "evidence": evidence_items,
        "findings": {
            "security": _latest_finding(evidence, "ASSURANCE_SECURITY"),
            "quality": _latest_finding(evidence, "ASSURANCE_QUALITY"),
        },
        "defects": defects,
        "defect_counts": {
            "by_severity": by_severity,
            "by_priority": by_priority,
            "by_status": by_status,
        },
        "lineage": _lineage(jobs, invalidations, edges),
        "release_blocking_reasons": _release_blocking_reasons(
            role_results=role_results,
            evidence=evidence,
            jobs=jobs,
            defects=defects,
        ),
    }
