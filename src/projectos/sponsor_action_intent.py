"""Deterministic Sponsor action-intent recognition (independent of Advisor formatting)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from projectos.request_capability import classify_request
from projectos.slack_advisor_handoff import looks_like_handoff_trigger

_RELEASE_ACTION_RE = re.compile(
    r"\b("
    r"re-?release|release the|package and installer|finished download|download link|"
    r"i want projectos to\b"
    r")",
    re.IGNORECASE,
)
_JOB_DETAIL_RE = re.compile(r"\b(JOB-[A-Z0-9_-]+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class SponsorActionIntent:
    kind: str
    requires_pm_handoff: bool
    request_type: str
    job_human_id: str | None = None


def detect_sponsor_action_intent(text: str) -> SponsorActionIntent:
    raw = str(text or "").strip()
    job_match = _JOB_DETAIL_RE.search(raw)
    job_id = job_match.group(1).upper() if job_match else None
    cap = classify_request(text=raw, fallback_objective=raw)
    if looks_like_handoff_trigger(raw):
        return SponsorActionIntent(
            kind="handoff_trigger",
            requires_pm_handoff=True,
            request_type=cap.request_type,
            job_human_id=job_id,
        )
    if cap.request_type == "RELEASE" or _RELEASE_ACTION_RE.search(raw):
        return SponsorActionIntent(
            kind="release_action",
            requires_pm_handoff=True,
            request_type="RELEASE",
            job_human_id=job_id,
        )
    if job_id and any(w in raw.lower() for w in ("blocked", "what was", "details", "remaining")):
        return SponsorActionIntent(
            kind="job_inquiry",
            requires_pm_handoff=False,
            request_type="QUALITY",
            job_human_id=job_id,
        )
    return SponsorActionIntent(
        kind="deliberation",
        requires_pm_handoff=False,
        request_type=cap.request_type,
        job_human_id=job_id,
    )
