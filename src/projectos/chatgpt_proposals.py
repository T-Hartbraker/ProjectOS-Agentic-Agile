"""Persisted ChatGPT → ProjectOS proposal state machine."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from projectos.errors import OrchestrationError

PROPOSAL_TTL_HOURS = 24
MAX_INSTRUCTION_CHARS = 2000
MAX_HUMAN_SUMMARY_CHARS = 500
MAX_RESULT_CHARS = 4000
MAX_PREVIEW_CHARS = 8000

PROPOSAL_REQUEST_RE = re.compile(
    r"```projectos_proposal\s*(\{.*?\})\s*```",
    re.IGNORECASE | re.DOTALL,
)
LEGACY_REQUEST_RE = re.compile(
    r"```projectos_request\s*(\{.*?\})\s*```",
    re.IGNORECASE | re.DOTALL,
)

READ_ONLY_INTENTS = frozenset({"status", "summary", "quality", "qa", "releases", "release"})
WORK_OUTCOME_INTENTS = frozenset({"work", "work_request"})
# Legacy model output — normalized to work_request at persistence time.
LEGACY_PREVIEW_INTENTS = frozenset({"work_preview"})
MUTATING_INTENTS = WORK_OUTCOME_INTENTS | LEGACY_PREVIEW_INTENTS | frozenset(
    {"prepare_release", "package_release", "publish_release"}
)
ALLOWED_INTENTS = READ_ONLY_INTENTS | MUTATING_INTENTS

APPROVAL_RE = re.compile(
    r"\b("
    r"do it|proceed|go ahead|approved|approve|run it|execute it|execute this|execute!?|"
    r"please do that|yes[, ]+do|make it so"
    r")\b",
    re.IGNORECASE,
)
NEGATED_APPROVAL_RE = re.compile(
    r"\b(?:do not|don't|not|without|never|no)\b.{0,40}\b(?:execute|approve|proceed|run it)\b",
    re.IGNORECASE,
)

ACTIVE_PROPOSAL_STATUSES = frozenset({"pending", "approved"})
TERMINAL_PROPOSAL_STATUSES = frozenset({"completed", "failed", "expired", "rejected", "dispatched"})


@dataclass(frozen=True)
class ProposalRequest:
    intent: str
    instruction: str


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    team_id: str
    channel_id: str
    thread_ts: str
    sponsor_user_id: str
    project_human_id: str
    intent: str
    action_type: str
    instruction: str
    human_summary: str
    status: str
    approval_message_ts: str | None
    preview_result: str | None
    preview_generated_at: str | None
    result_text: str | None
    risk: str
    scope: str
    created_at: str
    expires_at: str


def normalize_action_type(intent: str) -> str:
    """Map tool-level intents to Sponsor-approved outcome types."""
    normalized = str(intent or "").strip().lower()
    if normalized in LEGACY_PREVIEW_INTENTS | WORK_OUTCOME_INTENTS:
        return "work_request"
    return normalized


def is_work_mutation(action_type: str) -> bool:
    return str(action_type or "").strip().lower() == "work_request"


def human_summary_from_instruction(instruction: str) -> str:
    """Immutable sponsor-facing summary captured at proposal creation."""
    return str(instruction or "").strip()[:MAX_HUMAN_SUMMARY_CHARS]


def infer_risk_from_instruction(instruction: str) -> str:
    lowered = str(instruction or "").lower()
    if any(word in lowered for word in ("release", "publish", "production", "delete", "remove")):
        return "high"
    if any(word in lowered for word in ("code", "refactor", "deploy", "package")):
        return "medium"
    return "low"


def looks_like_approval(text: str) -> bool:
    raw = str(text or "")
    if NEGATED_APPROVAL_RE.search(raw):
        return False
    return bool(APPROVAL_RE.search(raw))


def strip_proposal_blocks(text: str) -> str:
    cleaned = PROPOSAL_REQUEST_RE.sub("", str(text or ""))
    return LEGACY_REQUEST_RE.sub("", cleaned).strip()


def parse_proposal_request(text: str) -> ProposalRequest | None:
    """Parse an untrusted model proposal request. Never returns executable authorization."""
    match = PROPOSAL_REQUEST_RE.search(str(text or "")) or LEGACY_REQUEST_RE.search(str(text or ""))
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    intent = str(payload.get("intent") or "").strip().lower()
    instruction = str(payload.get("instruction") or "").strip()
    if intent not in ALLOWED_INTENTS:
        return None
    if not instruction:
        return None
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        instruction = instruction[:MAX_INSTRUCTION_CHARS]
    return ProposalRequest(intent=intent, instruction=instruction)


def _expires_at(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return (moment + timedelta(hours=PROPOSAL_TTL_HOURS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_expired(expires_at: str) -> bool:
    try:
        raw = expires_at.replace("Z", "+00:00")
        return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _row_to_record(row: sqlite3.Row) -> ProposalRecord:
    intent = str(row["intent"] or "")
    action_type = str(row["action_type"] or "") if "action_type" in row.keys() else ""
    if not action_type:
        action_type = normalize_action_type(intent)
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        team_id=row["team_id"] or "",
        channel_id=row["channel_id"],
        thread_ts=row["thread_ts"] or "",
        sponsor_user_id=row["sponsor_user_id"],
        project_human_id=row["project_human_id"],
        intent=intent,
        action_type=action_type,
        instruction=row["instruction"],
        human_summary=str(row["human_summary"] or row["instruction"] or "")[:MAX_HUMAN_SUMMARY_CHARS],
        status=row["status"],
        approval_message_ts=row["approval_message_ts"],
        preview_result=row["preview_result"] if "preview_result" in row.keys() else None,
        preview_generated_at=row["preview_generated_at"] if "preview_generated_at" in row.keys() else None,
        result_text=row["result_text"] if "result_text" in row.keys() else None,
        risk=str(row["risk"] or "low") if "risk" in row.keys() else "low",
        scope=str(row["scope"] or "") if "scope" in row.keys() else "",
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def proposal_awaiting_approval(record: ProposalRecord) -> bool:
    return record.status == "pending" and bool(str(record.preview_result or "").strip())


def proposal_lifecycle_label(record: ProposalRecord) -> str:
    if record.status == "pending" and not record.preview_result:
        return "PROPOSED"
    if proposal_awaiting_approval(record):
        return "AWAITING_SPONSOR_APPROVAL"
    if record.status == "approved":
        return "APPROVED"
    if record.status == "dispatched":
        return "EXECUTING"
    if record.status == "completed":
        return "COMPLETED"
    if record.status == "failed":
        return "FAILED"
    if record.status == "expired":
        return "EXPIRED"
    return record.status.upper()


def create_proposal(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    project_human_id: str,
    intent: str,
    instruction: str,
) -> ProposalRecord:
    if not project_human_id:
        raise OrchestrationError("A resolved project is required before creating a proposal")
    raw_intent = str(intent or "").strip().lower()
    if raw_intent not in ALLOWED_INTENTS:
        raise OrchestrationError(f"Unsupported proposal intent: {raw_intent}")
    if raw_intent in READ_ONLY_INTENTS:
        raise OrchestrationError("Read-only ProjectOS operations do not require a proposal")
    instruction = str(instruction or "").strip()
    if not instruction:
        raise OrchestrationError("Proposal instruction is required")
    action_type = normalize_action_type(raw_intent)
    existing = list_pending_proposals(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        sponsor_user_id=sponsor_user_id,
    )
    for item in existing:
        if item.project_human_id == project_human_id:
            return item
    proposal_id = str(uuid.uuid4())
    expires = _expires_at()
    summary = human_summary_from_instruction(instruction)
    risk = infer_risk_from_instruction(instruction)
    scope = instruction[:240]
    conn.execute(
        """
        INSERT INTO slack_chatgpt_proposals (
            proposal_id, team_id, channel_id, thread_ts, sponsor_user_id,
            project_human_id, intent, action_type, instruction, human_summary,
            risk, scope, status, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            proposal_id,
            team_id or "",
            channel_id,
            thread_ts or "",
            sponsor_user_id,
            project_human_id,
            action_type,
            action_type,
            instruction[:MAX_INSTRUCTION_CHARS],
            summary,
            risk,
            scope,
            expires,
        ),
    )
    row = conn.execute(
        "SELECT * FROM slack_chatgpt_proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    assert row is not None
    return _row_to_record(row)


def save_proposal_preview(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    preview_result: str,
    risk: str | None = None,
    scope: str | None = None,
) -> ProposalRecord | None:
    conn.execute(
        """
        UPDATE slack_chatgpt_proposals
        SET preview_result = ?,
            preview_generated_at = datetime('now'),
            risk = COALESCE(?, risk),
            scope = COALESCE(?, scope)
        WHERE proposal_id = ? AND status = 'pending'
        """,
        (
            str(preview_result or "")[:MAX_PREVIEW_CHARS],
            risk,
            scope,
            proposal_id,
        ),
    )
    return get_proposal(conn, proposal_id)


def list_pending_proposals(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
) -> list[ProposalRecord]:
    rows = conn.execute(
        """
        SELECT * FROM slack_chatgpt_proposals
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
          AND sponsor_user_id = ? AND status = 'pending'
        ORDER BY created_at ASC
        """,
        (team_id or "", channel_id, thread_ts or "", sponsor_user_id),
    ).fetchall()
    out: list[ProposalRecord] = []
    for row in rows:
        record = _row_to_record(row)
        if _is_expired(record.expires_at):
            expire_proposal(conn, proposal_id=record.proposal_id)
            continue
        out.append(record)
    return out


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> ProposalRecord | None:
    row = conn.execute(
        "SELECT * FROM slack_chatgpt_proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    return _row_to_record(row) if row else None


def list_thread_proposals(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
) -> list[ProposalRecord]:
    rows = conn.execute(
        """
        SELECT * FROM slack_chatgpt_proposals
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
          AND sponsor_user_id = ?
        ORDER BY created_at ASC
        """,
        (team_id or "", channel_id, thread_ts or "", sponsor_user_id),
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def get_latest_thread_proposal(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
) -> ProposalRecord | None:
    proposals = list_thread_proposals(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        sponsor_user_id=sponsor_user_id,
    )
    return proposals[-1] if proposals else None


def expire_proposal(conn: sqlite3.Connection, *, proposal_id: str) -> None:
    conn.execute(
        """
        UPDATE slack_chatgpt_proposals
        SET status = 'expired'
        WHERE proposal_id = ? AND status = 'pending'
        """,
        (proposal_id,),
    )


def approve_pending_proposal(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    project_human_id: str | None,
    approval_message_ts: str,
    approval_text: str,
) -> tuple[ProposalRecord | None, str | None]:
    if not looks_like_approval(approval_text):
        return None, "That message does not look like explicit Sponsor approval."
    pending = list_pending_proposals(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        sponsor_user_id=sponsor_user_id,
    )
    if not pending:
        return None, "There is no pending ProjectOS proposal to approve in this thread."
    if len(pending) > 1:
        return None, "Multiple pending proposals exist. Ask ChatGPT to restate the exact action you are approving."
    proposal = pending[0]
    if project_human_id and proposal.project_human_id != project_human_id:
        return None, "Project context changed since the proposal was created. Ask ChatGPT to create a new proposal."
    if is_work_mutation(proposal.action_type) and not proposal.preview_result:
        return None, (
            "This proposal has not been previewed yet. "
            "Ask ChatGPT to show the preview before approving execution."
        )
    conn.execute(
        """
        UPDATE slack_chatgpt_proposals
        SET status = 'approved',
            approval_message_ts = ?
        WHERE proposal_id = ? AND status = 'pending'
        """,
        (approval_message_ts, proposal.proposal_id),
    )
    approved = get_proposal(conn, proposal.proposal_id)
    if approved is None or approved.status != "approved":
        return None, "Proposal could not be approved."
    return approved, None


def mark_proposal_dispatched(conn: sqlite3.Connection, *, proposal_id: str) -> bool:
    cur = conn.execute(
        """
        UPDATE slack_chatgpt_proposals
        SET status = 'dispatched', dispatched_at = datetime('now')
        WHERE proposal_id = ? AND status = 'approved'
        """,
        (proposal_id,),
    )
    return cur.rowcount == 1


def mark_proposal_completed(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    result_text: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE slack_chatgpt_proposals
        SET status = 'completed',
            completed_at = datetime('now'),
            last_error = NULL,
            result_text = COALESCE(?, result_text)
        WHERE proposal_id = ? AND status = 'dispatched'
        """,
        (str(result_text or "")[:MAX_RESULT_CHARS] or None, proposal_id),
    )


def mark_proposal_failed(conn: sqlite3.Connection, *, proposal_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE slack_chatgpt_proposals
        SET status = 'failed', completed_at = datetime('now'), last_error = ?
        WHERE proposal_id = ? AND status IN ('approved', 'dispatched')
        """,
        (str(error or "")[:240], proposal_id),
    )


def proposal_to_execution_payload(proposal: ProposalRecord) -> dict[str, Any]:
    """Trusted server-side execution payload. Never built from post-approval model text."""
    return {
        "source": "chatgpt",
        "proposal_id": proposal.proposal_id,
        "project_id": proposal.project_human_id,
        "sponsor_user_id": proposal.sponsor_user_id,
        "slack_channel_id": proposal.channel_id,
        "slack_thread_ts": proposal.thread_ts,
        "intent": proposal.action_type,
        "action_type": proposal.action_type,
        "instruction": proposal.instruction,
        "authorization": {
            "type": "explicit_sponsor_approval",
            "evidence_message_ts": proposal.approval_message_ts,
        },
    }
