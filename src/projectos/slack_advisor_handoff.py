"""Structured Advisor → ProjectOS handoff parsing and formatting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from projectos.chatgpt_proposals import normalize_action_type, strip_proposal_blocks

HANDOFF_REQUEST_RE = re.compile(
    r"```projectos_handoff\s*(\{.*?\})\s*```",
    re.IGNORECASE | re.DOTALL,
)
# Legacy block — only honored at explicit Sponsor action boundary.
LEGACY_PROPOSAL_IN_HANDOFF = re.compile(
    r"```projectos_proposal\s*(\{.*?\})\s*```",
    re.IGNORECASE | re.DOTALL,
)

HANDOFF_TRIGGER_RE = re.compile(
    r"\b("
    r"have projectos|have project os|send (?:that |this |it )?to projectos|send to project os|"
    r"yes[,.]? send (?:that |this |it )?to projectos|"
    r"create (?:that |the |this )?work|let'?s execute|go ahead with (?:that|this|option)|"
    r"prepare (?:that |this )?(?:for projectos)?|prepare it|submit this work|package the release|"
    r"create the project|okay[,.]? have projectos|ok[,.]? have projectos|"
    r"proceed with (?:that|this|option)|do that with projectos|execute!?|"
    r"have project os execute"
    r")\b",
    re.IGNORECASE,
)

MAX_FIELD_CHARS = 2000


@dataclass(frozen=True)
class HandoffRequest:
    project_id: str
    objective: str
    action_type: str
    rationale: str
    scope: str
    constraints: str
    acceptance_intent: str
    exclusions: str
    source_conversation_summary: str
    desired_outputs_json: str = ""

    def to_instruction(self) -> str:
        lines = [self.objective.strip()]
        if self.scope.strip():
            lines.append(f"Scope: {self.scope.strip()}")
        if self.constraints.strip():
            lines.append(f"Constraints: {self.constraints.strip()}")
        if self.acceptance_intent.strip():
            lines.append(f"Acceptance intent: {self.acceptance_intent.strip()}")
        if self.exclusions.strip():
            lines.append(f"Exclusions: {self.exclusions.strip()}")
        if self.rationale.strip():
            lines.append(f"Rationale: {self.rationale.strip()}")
        if self.source_conversation_summary.strip():
            lines.append(f"Conversation summary: {self.source_conversation_summary.strip()}")
        return "\n".join(lines)[:MAX_FIELD_CHARS]


def looks_like_handoff_trigger(text: str) -> bool:
    return bool(HANDOFF_TRIGGER_RE.search(str(text or "")))


def strip_handoff_blocks(text: str) -> str:
    cleaned = HANDOFF_REQUEST_RE.sub("", str(text or ""))
    cleaned = LEGACY_PROPOSAL_IN_HANDOFF.sub("", cleaned)
    return strip_proposal_blocks(cleaned).strip()


def _parse_handoff_payload(payload: dict) -> HandoffRequest | None:
    if not isinstance(payload, dict):
        return None
    objective = str(payload.get("objective") or payload.get("instruction") or "").strip()
    if not objective:
        return None
    action_type = normalize_action_type(
        str(payload.get("action_type") or payload.get("intent") or "work_request")
    )
    constraints_raw = str(payload.get("constraints") or "")[:MAX_FIELD_CHARS]
    from projectos.sponsor_execution_authority import strip_untrusted_authority_fields

    return HandoffRequest(
        project_id=str(payload.get("project_id") or "").strip().upper(),
        objective=objective[:MAX_FIELD_CHARS],
        action_type=action_type,
        rationale=str(payload.get("rationale") or "")[:MAX_FIELD_CHARS],
        scope=str(payload.get("scope") or "")[:MAX_FIELD_CHARS],
        constraints=strip_untrusted_authority_fields(constraints_raw)[:MAX_FIELD_CHARS],
        acceptance_intent=str(payload.get("acceptance_intent") or "")[:MAX_FIELD_CHARS],
        exclusions=str(payload.get("exclusions") or "")[:MAX_FIELD_CHARS],
        source_conversation_summary=str(payload.get("source_conversation_summary") or "")[
            :MAX_FIELD_CHARS
        ],
    )


def parse_handoff_request(text: str) -> HandoffRequest | None:
    raw = str(text or "")
    match = HANDOFF_REQUEST_RE.search(raw) or LEGACY_PROPOSAL_IN_HANDOFF.search(raw)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if LEGACY_PROPOSAL_IN_HANDOFF.search(raw) and isinstance(payload, dict):
        payload = {
            "objective": payload.get("instruction"),
            "action_type": payload.get("intent"),
            "source_conversation_summary": "Legacy proposal block converted at handoff boundary.",
        }
    return _parse_handoff_payload(payload)


def format_handoff_preamble(handoff: HandoffRequest, *, project_id: str) -> str:
    lines = [
        "We've reached the action boundary. Here is what I am asking ProjectOS to validate.",
        "",
        "*What I'm asking ProjectOS to do*",
        "",
        f"*Project:* {project_id}",
        "",
        "*Objective*",
        handoff.objective,
    ]
    if handoff.scope:
        lines.extend(["", "*Scope*", handoff.scope])
    if handoff.constraints:
        lines.extend(["", "*Constraints*", handoff.constraints])
    if handoff.exclusions:
        lines.extend(["", "*Exclusions*", handoff.exclusions])
    if handoff.acceptance_intent:
        lines.extend(["", "*Expected result*", handoff.acceptance_intent])
    lines.extend(
        [
            "",
            "I'll have ProjectOS validate this before anything changes. "
            "If governance requires it, you will need to approve the deterministic preview.",
        ]
    )
    return "\n".join(lines)
