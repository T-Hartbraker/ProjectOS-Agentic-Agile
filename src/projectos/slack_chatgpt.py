"""ChatGPT advisor bridge for Slack Socket Mode.

Thread context strategy
-----------------------
* OpenAI continuity uses Responses API ``previous_response_id`` chaining only.
* Local SQLite messages are retained for Sponsor approval evidence and audit.
* Each model call sends bounded authoritative Sponsor context plus the latest message.
* Proposal preview and execution are deterministic server-side operations.
"""

from __future__ import annotations

import json
import re
from typing import Any

from projectos.chatgpt_proposals import (
    TERMINAL_PROPOSAL_STATUSES,
    approve_pending_proposal,
    get_latest_thread_proposal,
    get_proposal,
    list_pending_proposals,
    looks_like_approval,
    mark_proposal_completed,
    mark_proposal_dispatched,
    mark_proposal_failed,
    normalize_action_type,
    proposal_awaiting_approval,
    proposal_lifecycle_label,
    save_proposal_preview,
)
from projectos.chatgpt_store import (
    get_chatgpt_thread,
    insert_chatgpt_message,
    upsert_chatgpt_thread,
)
from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.intake import IntakeResult, IntakeService
from projectos.migrate import initialize_database
from projectos.openai_client import HttpPost, create_response
from projectos.openai_config import openai_enabled
from projectos.openai_tokens import api_key, contains_secret
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt_config import (
    chatgpt_mention_pattern,
    chatgpt_slack_user_id,
    strip_chatgpt_mention,
)
from projectos.slack_event_routing import is_registered_interface_channel_event
from projectos.advisor_errors import (
    classify_advisor_exception,
    format_advisor_error_reply,
    new_error_id,
)
from projectos.chatgpt_store import list_chatgpt_messages
from projectos.pm_agent import accept_sponsor_handoff, compose_server_handoff
from projectos.pm_capabilities import ensure_pm_run_for_approved_proposal, execute_approved_proposal_via_pm
from projectos.sponsor_action_audit import record_sponsor_action_audit
from projectos.sponsor_action_intent import detect_sponsor_action_intent
from projectos.sponsor_query import SponsorQueryService
from projectos.slack_advisor_handoff import (
    looks_like_handoff_trigger,
    parse_handoff_request,
    strip_handoff_blocks,
)
from projectos.slack_resolver import (
    load_registry_or_empty,
    lookup_project_identifier,
    resolve_slack_project,
    set_session_project,
)
from projectos.slack_sponsor_blocks import dual_response, single_advisor_response
from projectos.slack_sponsor_context import build_sponsor_context
from projectos.slack_sponsor_format import (
    SPONSOR_ACCEPTANCE,
    format_execution_complete_advisor,
    format_work_intake_execution,
    format_work_intake_preview,
)

CHATGPT_PREFIX = "*ChatGPT Advisor:*"
PROJECTOS_PREFIX = "*ProjectOS:*"
_PROJECT_ID_RE = re.compile(r"\b(PRJ-[A-Z0-9]+)\b", re.IGNORECASE)
READ_ONLY_REQUEST_RE = re.compile(
    r"\b(summary|summarize|status|health|overview|quality|qa|releases?|"
    r"what needs to be done|why did it stop|what blocked|what do i need to fix|can we retry|"
    r"blocker|blocked|failed)\b",
    re.IGNORECASE,
)
BLOCKER_QUESTION_RE = re.compile(
    r"\b(what needs to be done|why did it stop|what blocked|what do i need to fix|can we retry)\b",
    re.IGNORECASE,
)
PROPOSAL_STATE_QUESTION_RE = re.compile(
    r"\b("
    r"what was proposed|summary of the proposed|proposed change|"
    r"show me the preview|what was changed|what did that do|what did you actually change|"
    r"was anything (?:actually )?modified|preview of what|more detailed preview|more detail|"
    r"let me preview|show.*preview|what(?:'s| is) the (?:summary|preview)"
    r")\b",
    re.IGNORECASE,
)
_FALSE_STATE_RE = re.compile(
    r"\b("
    r"projectos (?:has|have) (?:completed|executed|created|submitted)|"
    r"execution (?:has|is) (?:started|complete)|"
    r"i(?:'ve| have) (?:sent|submitted) (?:this )?to projectos|"
    r"proposal sent to projectos"
    r")\b",
    re.IGNORECASE,
)
MAX_SPONSOR_MESSAGE_CHARS = 4000
MAX_ADVISOR_OUTPUT_CHARS = 12000

SYSTEM_INSTRUCTIONS = """You are ChatGPT Advisor in a ProjectOS Slack workspace.

Architecture:
SPONSOR <-> CHATGPT ADVISOR (you) <-> PROJECTOS

Your role is DELIBERATION:
- reasoning, analysis, planning, interpretation, tradeoffs, recommendations
- help the Sponsor think before acting
- multi-paragraph answers are welcome when useful

ProjectOS role is CONTROL PLANE:
- authoritative project state, governance, execution, QA, release, audit
- you may reason ABOUT ProjectOS facts; you must NOT invent them

Authority boundaries:
YOU MAY: reason, recommend, explain, deliberate, compare options, challenge assumptions,
         formulate a proposed handoff when the Sponsor is ready to act.
YOU MAY NOT: mutate project state, invent ProjectOS IDs/state, claim work executed,
             approve governance on behalf of the Sponsor, fabricate evidence.

Conversation mode (default):
Most Sponsor messages are deliberation. Respond thoughtfully with context from the
Authoritative ProjectOS context block. Explain WHY, compare options, note risks.
Discussion is NOT authorization.

Explicit handoff boundary:
Only when the Sponsor clearly wants ProjectOS to act (e.g. "Have ProjectOS do that",
"Send that to ProjectOS", "Let's execute this", "Submit this work"), include a handoff block:

```projectos_handoff
{
  "objective": "...",
  "action_type": "work_request",
  "rationale": "...",
  "scope": "...",
  "constraints": "...",
  "acceptance_intent": "...",
  "exclusions": "...",
  "source_conversation_summary": "..."
}
```

Do NOT include handoff blocks during ordinary deliberation.
Do NOT include project IDs, sponsor IDs, or authorization in the handoff.
ProjectOS will validate, preview, and obtain Sponsor approval per governance.

Never claim ProjectOS executed work unless execution evidence is provided in context.
Label recommendations clearly when they are not yet ProjectOS state.

Fact boundaries in context:
- [AUTHORITATIVE FACTS] and [AUTHORITATIVE JOB FACTS] are ground truth.
- [KNOWN UNKNOWNS] lists what ProjectOS does not know — never fill these gaps as facts.
- [ADVISOR INFERENCES] is where you may reason, clearly labeled as inference.
- [RECOMMENDATIONS] are not execution.

If a job is blocked/failed and no last_error is recorded, say:
"ProjectOS does not contain enough evidence to determine the exact cause."
You may note that a later successful retry suggests recovery, but that does not prove
the original underlying condition was formally resolved.

Never substitute assurance evidence row counts for QA job counts or test counts.

Execution awareness:
- If [ACTIVE RUN] or [TERMINAL RUN] shows handoff ACCEPTED_BY_PM or a RUN-* exists,
  ProjectOS has already accepted the Sponsor request. Do NOT ask whether to initiate the same action.
- State: "ProjectOS has accepted the release request and started RUN-..." then interpret current state.
- For blocker questions (what needs to be done, why did it stop, what blocked, what to fix, can we retry),
  prioritize [TERMINAL BLOCKER EVIDENCE] and [TERMINAL RUN] before generic QA/process advice.
"""


def is_chatgpt_addressed(text: str, *, event: dict[str, Any] | None = None) -> bool:
    uid = chatgpt_slack_user_id()
    if not uid:
        return False
    if chatgpt_mention_pattern(uid).search(str(text or "")):
        return True
    if event is not None and _event_mentions_user(event, uid):
        return True
    return False


def _event_mentions_user(event: dict[str, Any], user_id: str) -> bool:
    uid = str(user_id or "").strip().upper()
    if not uid:
        return False
    for block in event.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for element in _iter_rich_text_elements(block):
            if str(element.get("type") or "") == "user" and str(element.get("user_id") or "").upper() == uid:
                return True
    return False


def _iter_rich_text_elements(block: dict[str, Any]):
    elements = block.get("elements")
    if not isinstance(elements, list):
        return
    for element in elements:
        if not isinstance(element, dict):
            continue
        yield element
        nested = element.get("elements")
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, dict):
                    yield child


def _strip_chatgpt_address(text: str, *, event: dict[str, Any] | None = None) -> str:
    return strip_chatgpt_mention(text, event=event)


def _explicit_project_from_text(text: str) -> str | None:
    match = _PROJECT_ID_RE.search(str(text or ""))
    return match.group(1).upper() if match else None


def _looks_like_read_only_request(text: str) -> bool:
    if looks_like_handoff_trigger(text):
        return False
    if looks_like_approval(text):
        return False
    if _looks_like_proposal_state_question(text):
        return False
    return bool(READ_ONLY_REQUEST_RE.search(str(text or "")))


def _read_only_intent(text: str) -> str:
    if BLOCKER_QUESTION_RE.search(str(text or "")):
        return "blocker"
    if re.search(r"\bJOB-[A-Z0-9_-]+\b", str(text or ""), re.IGNORECASE):
        return "job"
    lowered = str(text or "").lower()
    if "quality" in lowered or re.search(r"\bqa\b", lowered):
        return "quality"
    if "release" in lowered:
        return "releases"
    if "status" in lowered or "health" in lowered:
        return "status"
    return "summary"


def _persist_sponsor_project_context(
    conn,
    *,
    team_id: str | None,
    channel_id: str,
    user_id: str,
    project_id: str,
    thread_key: str,
) -> None:
    set_session_project(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_key,
        user_id=user_id,
        project_human_id=project_id,
    )
    set_session_project(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts="",
        user_id=user_id,
        project_human_id=project_id,
    )


def _sanitize_advisor_text(text: str) -> str:
    cleaned = strip_handoff_blocks(str(text or ""))
    lines = [line for line in cleaned.splitlines() if not _FALSE_STATE_RE.search(line)]
    return "\n".join(lines).strip()[:MAX_ADVISOR_OUTPUT_CHARS]


def _resolve_authoritative_project(
    conn,
    *,
    registry_path,
    channel_id: str,
    team_id: str | None,
    thread_key: str,
    user_id: str,
    thread: dict[str, Any] | None,
    cleaned: str,
) -> tuple[str | None, str | None]:
    explicit = _explicit_project_from_text(cleaned)
    stored = str((thread or {}).get("project_human_id") or "").strip()
    registry = load_registry_or_empty(registry_path)

    if explicit:
        project_id, error = lookup_project_identifier(registry, explicit)
        if error:
            return None, error
        _persist_sponsor_project_context(
            conn,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            project_id=project_id,
            thread_key=thread_key,
        )
        return project_id, None

    if stored:
        project_id, error = lookup_project_identifier(registry, stored)
        if project_id:
            return project_id, None

    from projectos.chatgpt_store import get_recent_chatgpt_project

    recent = get_recent_chatgpt_project(
        conn,
        team_id=team_id or "",
        channel_id=channel_id,
        sponsor_user_id=user_id,
        exclude_thread_ts=thread_key,
    )
    if recent:
        project_id, error = lookup_project_identifier(registry, recent)
        if project_id:
            return project_id, None

    resolved = resolve_slack_project(
        conn,
        registry_path=registry_path,
        channel_id=channel_id,
        team_id=team_id,
        thread_ts=thread_key,
        user_id=user_id,
        explicit_project=None,
        require_channel_auth=True,
    )
    if resolved.unauthorized:
        return None, resolved.unauthorized_text or "This channel is not authorized for ProjectOS."
    if resolved.ok and resolved.project_human_id:
        _persist_sponsor_project_context(
            conn,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            project_id=resolved.project_human_id,
            thread_key=thread_key,
        )
        return resolved.project_human_id, None
    if resolved.unknown_project:
        return None, resolved.unknown_text
    if resolved.clarify:
        return None, resolved.clarify_text
    return None, "Use `/projectos use PRJ-###` in this thread so ProjectOS can resolve the project."


def _looks_like_proposal_state_question(text: str) -> bool:
    if looks_like_approval(text):
        return False
    return bool(PROPOSAL_STATE_QUESTION_RE.search(str(text or "")))


def _work_intake_kwargs(proposal) -> dict[str, str]:
    objective = str(proposal.instruction or "").strip() or "Execute the Sponsor-approved action."
    return {
        "business_request": objective,
        "objective": objective,
        "acceptance": SPONSOR_ACCEPTANCE,
        "sponsor_authority": "approved",
    }


def preview_work_proposal(ctx: ServiceContext, proposal) -> tuple[IntakeResult, str]:
    kwargs = _work_intake_kwargs(proposal)
    result = IntakeService(ctx).preview(proposal.project_human_id, **kwargs)
    text = format_work_intake_preview(result, proposal)
    return result, text


def execute_work_proposal(ctx: ServiceContext, proposal) -> tuple[IntakeResult, str]:
    kwargs = _work_intake_kwargs(proposal)
    result = IntakeService(ctx).submit(proposal.project_human_id, **kwargs)
    text = format_work_intake_execution(result, proposal)
    return result, text


def _generate_and_persist_preview(ctx: ServiceContext, conn, proposal) -> str:
    _, preview_text = preview_work_proposal(ctx, proposal)
    updated = save_proposal_preview(
        conn,
        proposal_id=proposal.proposal_id,
        preview_result=preview_text,
        risk=proposal.risk,
        scope=proposal.scope,
    )
    if updated is None:
        raise OrchestrationError("Proposal preview could not be persisted")
    return preview_text


def _build_authoritative_context(
    ctx: ServiceContext,
    conn,
    *,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
    extra_facts: str = "",
) -> str:
    from projectos.domain_events import lookup_event_context_for_project
    from projectos.orchestration_boundary import run_with_internal_defect_routing

    event_ctx = lookup_event_context_for_project(conn, project_id)

    def _build():
        return build_sponsor_context(
            ctx,
            conn,
            project_id=project_id,
            team_id=team_id,
            channel_id=channel_id,
            thread_key=thread_key,
            sponsor_user_id=sponsor_user_id,
        )

    if event_ctx is not None:
        sponsor_ctx = run_with_internal_defect_routing(
            conn,
            event_ctx=event_ctx,
            project_id=project_id,
            component="slack_sponsor_context",
            operation="build_sponsor_context",
            fn=_build,
        )
    else:
        sponsor_ctx = _build()
    lines = [sponsor_ctx.to_model_text()]
    if extra_facts.strip():
        lines.append("")
        lines.append("[FRESH PROJECTOS FETCH]")
        lines.append(extra_facts.strip())
    return "\n\n".join(lines)


def _answer_proposal_state_question(
    conn,
    *,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
    cleaned: str,
) -> tuple[str, str | None]:
    latest = get_latest_thread_proposal(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_key,
        sponsor_user_id=sponsor_user_id,
    )
    if latest is None:
        return "There is no ProjectOS proposal in this thread yet.", None
    lowered = str(cleaned or "").lower()
    if latest.result_text and any(
        token in lowered for token in ("changed", "modified", "actually", "executed", "did that do")
    ):
        advisor = (
            f"Here is the authoritative execution evidence for proposal `{latest.proposal_id}`."
        )
        return advisor, latest.result_text
    if latest.preview_result:
        advisor = (
            f"Here is the persisted ProjectOS preview for proposal `{latest.proposal_id}` "
            f"({proposal_lifecycle_label(latest)}). No model-generated details are included."
        )
        return advisor, latest.preview_result
    if latest.status == "pending":
        advisor = (
            f"Proposal `{latest.proposal_id}` is pending. "
            "ProjectOS has not generated a preview yet."
        )
        return advisor, f"*Exact instruction*\n{latest.instruction}"
    advisor = (
        f"Proposal `{latest.proposal_id}` has status `{latest.status}` "
        "but no persisted ProjectOS preview or execution evidence was recorded."
    )
    return advisor, f"*Exact instruction*\n{latest.instruction}"


def _flush_projectos_outbox(ctx: ServiceContext, http_post: HttpPost | None = None) -> None:
    from projectos.event_dispatcher import dispatch_event_outbox

    dispatch_event_outbox(ctx.db_path, http_post=http_post)


def _fetch_read_only_projectos_data(ctx: ServiceContext, *, project_id: str, cleaned: str) -> str:
    intent = _read_only_intent(cleaned)
    return SponsorQueryService(ctx).query_for_advisor(project_id, intent)


def _thread_key(thread_ts: str | None, message_ts: str | None) -> str:
    return str(thread_ts or message_ts or "").strip()


def _openai_unavailable_reply(*, reason: str) -> dict[str, Any]:
    return {
        "text": (
            f"{PROJECTOS_PREFIX}\n"
            "ChatGPT Advisor is currently unavailable.\n"
            f"{reason}\n"
            "Check Settings → Integrations → OpenAI."
        ),
        "response_type": "in_channel",
    }


def should_route_to_chatgpt(
    *,
    text: str,
    event: dict[str, Any],
    thread_state: dict[str, Any] | None,
    projectos_thread_active: bool = False,
    registered_channel_ids: set[str] | frozenset[str] | None = None,
) -> bool:
    if not is_registered_interface_channel_event(
        event, registered_channel_ids=registered_channel_ids
    ):
        return False
    if is_chatgpt_addressed(text, event=event):
        return True
    if projectos_thread_active and not is_chatgpt_addressed(text, event=event):
        return False
    if not openai_enabled() or not api_key():
        return False
    if thread_state and thread_state.get("active"):
        return str(event.get("type") or "") in {"message", "app_mention"}
    return False


def execute_projectos_proposal(
    ctx: ServiceContext,
    proposal: dict[str, Any],
) -> str:
    """Read-only ProjectOS fact retrieval for Advisor context (no mutations)."""
    project_id = str(proposal.get("project_id") or "").strip()
    if not project_id:
        raise OrchestrationError("project_id is required for ProjectOS execution")
    intent = str(proposal.get("intent") or proposal.get("action_type") or "status").strip().lower()
    intent = normalize_action_type(intent)
    mutation_intents = {
        "work_request",
        "prepare_release",
        "package_release",
        "publish_release",
    }
    if intent in mutation_intents:
        raise OrchestrationError(
            "Sponsor mutations must use SponsorHandoff and PM Agent orchestration."
        )
    return SponsorQueryService(ctx).query_for_advisor(project_id, intent)


def _build_model_input(
    *,
    authoritative_context: str,
    sponsor_message: str,
) -> str:
    lines = [authoritative_context]
    lines.append(f"Latest sponsor message:\n{sponsor_message[:MAX_SPONSOR_MESSAGE_CHARS]}")
    return "\n\n".join(lines)


def _optional_fresh_projectos_facts(ctx: ServiceContext, *, project_id: str, cleaned: str) -> str:
    if not _looks_like_read_only_request(cleaned) and not re.search(
        r"\bJOB-[A-Z0-9_-]+\b", cleaned, re.IGNORECASE
    ):
        return ""
    intent = _read_only_intent(cleaned)
    return SponsorQueryService(ctx).query_for_advisor(project_id, intent, raw_text=cleaned)


def _safe_build_authoritative_context(
    ctx: ServiceContext,
    conn,
    *,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
    extra_facts: str = "",
) -> str:
    try:
        return _build_authoritative_context(
            ctx,
            conn,
            project_id=project_id,
            team_id=team_id,
            channel_id=channel_id,
            thread_key=thread_key,
            sponsor_user_id=sponsor_user_id,
            extra_facts=extra_facts,
        )
    except Exception as exc:
        classified = classify_advisor_exception(exc, stage="Sponsor context assembly")
        record_sponsor_action_audit(
            conn,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_key,
            sponsor_user_id=sponsor_user_id,
            message_text="",
            project_human_id=project_id,
            failure_stage=classified.stage,
            error=classified,
        )
        return (
            f"=== AUTHORITATIVE PROJECTOS CONTEXT ===\n"
            f"[AUTHORITATIVE FACTS]\nProject ID: {project_id}\n"
            f"[KNOWN UNKNOWNS]\n- Full context assembly failed: {classified.detail[:200]}\n"
            "=== END PROJECTOS CONTEXT ==="
        )


def _attempt_resilient_handoff(
    ctx: ServiceContext,
    conn,
    *,
    cleaned: str,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
    advisor_text: str = "",
) -> tuple[str, str | None, bool]:
    thread_msgs = [
        str(m.get("text") or "")
        for m in list_chatgpt_messages(
            conn,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_key,
            limit=20,
        )
        if str(m.get("role") or "") == "sponsor"
    ]
    handoff = compose_server_handoff(
        project_id=project_id,
        sponsor_message=cleaned,
        thread_messages=thread_msgs,
        advisor_summary=advisor_text,
    )
    advisor_note, projectos_result = _process_handoff(
        ctx,
        conn,
        handoff=handoff,
        project_id=project_id,
        team_id=team_id,
        channel_id=channel_id,
        thread_key=thread_key,
        sponsor_user_id=sponsor_user_id,
        advisor_text=advisor_text or "Proceeding with governed PM handoff.",
    )
    return advisor_note, projectos_result, True


def _interpret_execution_result(
    *,
    project_id: str,
    projectos_result: str,
    response_id: str | None,
    http_post: HttpPost | None,
) -> tuple[str, str | None]:
    """Advisor interprets authoritative ProjectOS execution evidence (one OpenAI call)."""
    model_input = (
        "ProjectOS has completed a governed action. Interpret the evidence below for the Sponsor. "
        "Explain what happened, whether it matched intent, and sensible next considerations. "
        "Do not invent IDs, tasks, or state beyond the evidence.\n\n"
        f"=== PROJECTOS EXECUTION EVIDENCE ===\n{projectos_result}\n=== END EVIDENCE ==="
    )
    try:
        ai = create_response(
            instructions=SYSTEM_INSTRUCTIONS,
            user_input=model_input,
            previous_response_id=response_id,
            http_post=http_post,
        )
        text = _sanitize_advisor_text(ai.text) or (
            f"ProjectOS completed the action for {project_id}. See the execution evidence below."
        )
        return text, ai.response_id or response_id
    except OrchestrationError:
        return (
            f"ProjectOS completed the action for {project_id}. See the execution evidence below.",
            response_id,
        )


def _process_handoff(
    ctx: ServiceContext,
    conn,
    *,
    handoff,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
    advisor_text: str,
) -> tuple[str, str | None]:
    try:
        result = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=handoff,
            project_id=project_id,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_key,
            sponsor_user_id=sponsor_user_id,
            advisor_text=_sanitize_advisor_text(advisor_text),
        )
    except OrchestrationError as exc:
        return _sanitize_advisor_text(advisor_text), (
            f"*ProjectOS — HANDOFF FAILED*\n{exc}"
        )
    projectos_parts = [result.projectos_text]
    if result.execution_evidence:
        projectos_parts.append(str(result.execution_evidence))
    return result.advisor_note, "\n\n".join(projectos_parts)


def _dispatch_approved_proposal(
    ctx: ServiceContext,
    conn,
    proposal,
    *,
    previous_response_id: str | None,
) -> tuple[str, str | None, str | None]:
    if not mark_proposal_dispatched(conn, proposal_id=proposal.proposal_id):
        raise OrchestrationError("Proposal was already dispatched or is no longer approved")
    try:
        run_id, _ = ensure_pm_run_for_approved_proposal(ctx, conn, proposal=proposal)
        projectos_result = execute_approved_proposal_via_pm(
            ctx,
            conn,
            proposal=proposal,
            run_id=run_id,
        )
        mark_proposal_completed(
            conn,
            proposal_id=proposal.proposal_id,
            result_text=projectos_result,
        )
    except OrchestrationError as exc:
        mark_proposal_failed(conn, proposal_id=proposal.proposal_id, error=str(exc))
        raise
    advisor_text = format_execution_complete_advisor(proposal)
    return advisor_text, projectos_result, previous_response_id


def _persist_turn(
    conn,
    *,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
    project_id: str,
    message_ts: str,
    advisor_text: str,
    projectos_text: str | None,
    response_id: str | None,
) -> None:
    if projectos_text:
        insert_chatgpt_message(
            conn,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_key,
            message_ts=f"projectos-{message_ts}",
            user_id=None,
            role="projectos",
            text=projectos_text,
        )
    insert_chatgpt_message(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_key,
        message_ts=f"chatgpt-{message_ts}",
        user_id=None,
        role="chatgpt",
        text=advisor_text,
    )
    upsert_chatgpt_thread(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_key,
        sponsor_user_id=sponsor_user_id,
        project_human_id=project_id,
        openai_response_id=response_id,
        active=True,
    )


def handle_chatgpt_slack_message(
    ctx: ServiceContext,
    *,
    text: str,
    channel_id: str,
    team_id: str | None,
    thread_ts: str | None,
    message_ts: str | None,
    user_id: str | None,
    http_post: HttpPost | None = None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not openai_enabled():
        return _openai_unavailable_reply(reason="OpenAI integration: disabled.")
    if not api_key():
        return _openai_unavailable_reply(reason="OpenAI integration: not configured.")
    if not user_id:
        return single_advisor_response("Could not identify the Slack user for this message.")

    initialize_database(ctx.db_path)
    thread_key = _thread_key(thread_ts, message_ts)
    cleaned = _strip_chatgpt_address(text, event=event)

    with connection(ctx.db_path) as conn:
        thread = get_chatgpt_thread(
            conn, team_id=team_id or "", channel_id=channel_id, thread_ts=thread_key
        )
        if thread is None:
            thread = upsert_chatgpt_thread(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_ts=thread_key,
                sponsor_user_id=user_id,
                active=True,
            )
        if not insert_chatgpt_message(
            conn,
            team_id=team_id or "",
            channel_id=channel_id,
            thread_ts=thread_key,
            message_ts=str(message_ts or thread_key),
            user_id=user_id,
            role="sponsor",
            text=cleaned,
        ):
            return None

        project_id, resolve_error = _resolve_authoritative_project(
            conn,
            registry_path=ctx.registry_path,
            channel_id=channel_id,
            team_id=team_id,
            thread_key=thread_key,
            user_id=user_id,
            thread=thread,
            cleaned=cleaned,
        )
        if resolve_error:
            upsert_chatgpt_thread(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_ts=thread_key,
                sponsor_user_id=thread["sponsor_user_id"],
                project_human_id=project_id,
                active=True,
            )
            return single_advisor_response(resolve_error)
        if not project_id:
            return single_advisor_response(
                "I need a resolved project before continuing. "
                "Use `/projectos use PRJ-###` in this thread."
            )

        upsert_chatgpt_thread(
            conn,
            team_id=team_id or "",
            channel_id=channel_id,
            thread_ts=thread_key,
            sponsor_user_id=thread["sponsor_user_id"],
            project_human_id=project_id,
            active=True,
        )

        response_id = thread.get("openai_response_id")

        if _looks_like_proposal_state_question(cleaned):
            advisor_text, projectos_text = _answer_proposal_state_question(
                conn,
                project_id=project_id,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_key=thread_key,
                sponsor_user_id=user_id,
                cleaned=cleaned,
            )
            _persist_turn(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_key=thread_key,
                sponsor_user_id=thread["sponsor_user_id"],
                project_id=project_id,
                message_ts=str(message_ts or thread_key),
                advisor_text=advisor_text,
                projectos_text=projectos_text,
                response_id=response_id,
            )
            return dual_response(advisor_text=advisor_text, projectos_text=projectos_text)

        pending_for_approval = list_pending_proposals(
            conn,
            team_id=team_id or "",
            channel_id=channel_id,
            thread_ts=thread_key,
            sponsor_user_id=user_id,
        )

        if looks_like_approval(cleaned) and pending_for_approval:
            approved, approval_error = approve_pending_proposal(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_ts=thread_key,
                sponsor_user_id=user_id,
                project_human_id=project_id,
                approval_message_ts=str(message_ts or thread_key),
                approval_text=cleaned,
            )
            if approval_error and not approved:
                _persist_turn(
                    conn,
                    team_id=team_id or "",
                    channel_id=channel_id,
                    thread_key=thread_key,
                    sponsor_user_id=thread["sponsor_user_id"],
                    project_id=project_id,
                    message_ts=str(message_ts or thread_key),
                    advisor_text=approval_error,
                    projectos_text=None,
                    response_id=response_id,
                )
                return single_advisor_response(approval_error)
            if approved is not None:
                advisor_text = ""
                projectos_result: str | None = None
                try:
                    _, projectos_result, response_id = _dispatch_approved_proposal(
                        ctx,
                        conn,
                        approved,
                        previous_response_id=response_id,
                    )
                    advisor_text, response_id = _interpret_execution_result(
                        project_id=project_id,
                        projectos_result=projectos_result or "",
                        response_id=response_id,
                        http_post=http_post,
                    )
                except OrchestrationError as exc:
                    advisor_text = str(exc)
                _persist_turn(
                    conn,
                    team_id=team_id or "",
                    channel_id=channel_id,
                    thread_key=thread_key,
                    sponsor_user_id=thread["sponsor_user_id"],
                    project_id=project_id,
                    message_ts=str(message_ts or thread_key),
                    advisor_text=advisor_text,
                    projectos_text=projectos_result,
                    response_id=response_id,
                )
                return dual_response(advisor_text=advisor_text, projectos_text=projectos_result)

        if looks_like_approval(cleaned) and not pending_for_approval:
            msg = "There is no pending ProjectOS proposal to approve in this thread."
            _persist_turn(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_key=thread_key,
                sponsor_user_id=thread["sponsor_user_id"],
                project_id=project_id,
                message_ts=str(message_ts or thread_key),
                advisor_text=msg,
                projectos_text=None,
                response_id=response_id,
            )
            return single_advisor_response(msg)

        fresh_facts = ""
        action_intent = detect_sponsor_action_intent(cleaned)
        try:
            fresh_facts = _optional_fresh_projectos_facts(ctx, project_id=project_id, cleaned=cleaned)
        except Exception as exc:
            classified = classify_advisor_exception(exc, stage="Sponsor context / release formatting")
            record_sponsor_action_audit(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_ts=thread_key,
                sponsor_user_id=user_id,
                message_text=cleaned,
                project_human_id=project_id,
                project_resolution=project_id,
                action_intent=action_intent.kind,
                failure_stage=classified.stage,
                error=classified,
            )
        authoritative_context = _safe_build_authoritative_context(
            ctx,
            conn,
            project_id=project_id,
            team_id=team_id or "",
            channel_id=channel_id,
            thread_key=thread_key,
            sponsor_user_id=user_id,
            extra_facts=fresh_facts,
        )
        model_input = _build_model_input(
            authoritative_context=authoritative_context,
            sponsor_message=cleaned,
        )
        try:
            ai = create_response(
                instructions=SYSTEM_INSTRUCTIONS,
                user_input=model_input,
                previous_response_id=response_id,
                http_post=http_post,
            )
        except OrchestrationError as exc:
            detail = str(exc)
            if contains_secret(detail):
                detail = "OpenAI request failed"
            upsert_chatgpt_thread(
                conn,
                team_id=team_id or "",
                channel_id=channel_id,
                thread_ts=thread_key,
                sponsor_user_id=thread["sponsor_user_id"],
                project_human_id=project_id,
                active=True,
                last_error=detail,
            )
            if action_intent.requires_pm_handoff:
                try:
                    advisor_text, projectos_result = _attempt_resilient_handoff(
                        ctx,
                        conn,
                        cleaned=cleaned,
                        project_id=project_id,
                        team_id=team_id or "",
                        channel_id=channel_id,
                        thread_key=thread_key,
                        sponsor_user_id=thread["sponsor_user_id"],
                    )[:2]
                    _persist_turn(
                        conn,
                        team_id=team_id or "",
                        channel_id=channel_id,
                        thread_key=thread_key,
                        sponsor_user_id=thread["sponsor_user_id"],
                        project_id=project_id,
                        message_ts=str(message_ts or thread_key),
                        advisor_text=advisor_text,
                        projectos_text=projectos_result,
                        response_id=response_id,
                    )
                    return dual_response(advisor_text=advisor_text, projectos_text=projectos_result)
                except Exception:
                    pass
            return single_advisor_response(detail)

        advisor_text = _sanitize_advisor_text(ai.text)
        projectos_result: str | None = fresh_facts or None
        if fresh_facts and not advisor_text:
            advisor_text = (
                f"Here is my read on {project_id} based on the latest ProjectOS facts below."
            )

        if looks_like_handoff_trigger(cleaned) or action_intent.requires_pm_handoff:
            handoff = parse_handoff_request(ai.text)
            if handoff is None:
                thread_msgs = [
                    str(m.get("text") or "")
                    for m in list_chatgpt_messages(
                        conn,
                        team_id=team_id or "",
                        channel_id=channel_id,
                        thread_ts=thread_key,
                        limit=20,
                    )
                    if str(m.get("role") or "") == "sponsor"
                ]
                handoff = compose_server_handoff(
                    project_id=project_id,
                    sponsor_message=cleaned,
                    thread_messages=thread_msgs,
                    advisor_summary=advisor_text,
                )
            try:
                advisor_text, projectos_result = _process_handoff(
                    ctx,
                    conn,
                    handoff=handoff,
                    project_id=project_id,
                    team_id=team_id or "",
                    channel_id=channel_id,
                    thread_key=thread_key,
                    sponsor_user_id=thread["sponsor_user_id"],
                    advisor_text=advisor_text,
                )
                record_sponsor_action_audit(
                    conn,
                    team_id=team_id or "",
                    channel_id=channel_id,
                    thread_ts=thread_key,
                    sponsor_user_id=user_id,
                    message_text=cleaned,
                    project_human_id=project_id,
                    project_resolution=project_id,
                    action_intent=action_intent.kind,
                    handoff_attempted=True,
                    pm_reached=True,
                )
            except OrchestrationError as exc:
                classified = classify_advisor_exception(exc, stage="PM handoff")
                error_id = record_sponsor_action_audit(
                    conn,
                    team_id=team_id or "",
                    channel_id=channel_id,
                    thread_ts=thread_key,
                    sponsor_user_id=user_id,
                    message_text=cleaned,
                    project_human_id=project_id,
                    project_resolution=project_id,
                    action_intent=action_intent.kind,
                    handoff_attempted=True,
                    failure_stage=classified.stage,
                    error=classified,
                )
                advisor_text = str(exc)
                projectos_result = f"*ProjectOS — HANDOFF FAILED*\n{exc}\nReference: `{error_id}`"

        _persist_turn(
            conn,
            team_id=team_id or "",
            channel_id=channel_id,
            thread_key=thread_key,
            sponsor_user_id=thread["sponsor_user_id"],
            project_id=project_id,
            message_ts=str(message_ts or thread_key),
            advisor_text=advisor_text or "What would you like to explore for this project?",
            projectos_text=projectos_result,
            response_id=ai.response_id or response_id,
        )
        result = dual_response(
            advisor_text=advisor_text or "What would you like to explore for this project?",
            projectos_text=projectos_result,
        )

    _flush_projectos_outbox(ctx, http_post=http_post)
    return result


def try_handle_chatgpt_event(
    ctx: ServiceContext,
    *,
    event: dict[str, Any],
    payload: dict[str, Any],
    bot_user_id: str | None = None,
    http_post: HttpPost | None = None,
    projectos_thread_active: bool = False,
    registered_channel_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any] | None:
    text = str(event.get("text") or "").strip()
    if not text:
        return None
    channel_id = str(event.get("channel") or "").strip()
    if not channel_id:
        return None
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip() or None
    message_ts = str(event.get("ts") or "").strip() or None
    user_id = str(event.get("user") or "").strip() or None

    if text.startswith(CHATGPT_PREFIX) or text.startswith(PROJECTOS_PREFIX):
        return None
    if user_id and user_id.upper() == chatgpt_slack_user_id().upper():
        return None

    initialize_database(ctx.db_path)
    thread_key = _thread_key(thread_ts, message_ts)
    with connection(ctx.db_path) as conn:
        thread_state = get_chatgpt_thread(
            conn, team_id=str(payload.get("team_id") or ""), channel_id=channel_id, thread_ts=thread_key
        )
    if not should_route_to_chatgpt(
        text=text,
        event=event,
        thread_state=thread_state,
        projectos_thread_active=projectos_thread_active,
        registered_channel_ids=registered_channel_ids,
    ):
        return None
    try:
        return handle_chatgpt_slack_message(
            ctx,
            text=text,
            channel_id=channel_id,
            team_id=str(payload.get("team_id") or "").strip() or None,
            thread_ts=thread_ts,
            message_ts=message_ts,
            user_id=user_id,
            http_post=http_post,
            event=event,
        )
    except Exception as exc:  # noqa: BLE001
        classified = classify_advisor_exception(exc)
        error_id = new_error_id()
        try:
            with connection(ctx.db_path) as conn:
                record_sponsor_action_audit(
                    conn,
                    team_id=str(payload.get("team_id") or ""),
                    channel_id=channel_id,
                    thread_ts=thread_key,
                    sponsor_user_id=user_id or "",
                    message_text=text,
                    failure_stage=classified.stage,
                    error=classified,
                    error_id=error_id,
                )
        except Exception:
            pass
        if classified.error_class.startswith("OPENAI_"):
            detail = classified.detail
            if contains_secret(detail):
                detail = "OpenAI request failed"
            return _openai_unavailable_reply(reason=f"OpenAI integration: {detail}")
        return format_advisor_error_reply(classified, error_id=error_id)
