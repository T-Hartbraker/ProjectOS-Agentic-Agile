"""Slack Socket Mode: outbound WebSocket, no public Request URL."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from projectos.db import connection
from projectos.errors import ConflictError, OrchestrationError
from projectos.migrate import initialize_database
from projectos.registry import load_registry_or_empty
from projectos.repository import load_repository_identity
from projectos.services.context import ServiceContext
from projectos.slack_event_routing import (
    is_app_mention_event,
    is_dm_message_event,
    is_private_channel_message_event,
    should_route_projectos_thread_followup,
    thread_ts_for_event,
)
from projectos.slack_thread_context import (
    is_projectos_thread_active,
    mark_projectos_thread_active,
    thread_root_ts,
)
from projectos.slack_chatgpt import try_handle_chatgpt_event
from projectos.slack_event_idempotency import slack_event_dedup_keys
from projectos.slack_commands import run_command
from projectos.slack_replies import (
    HELP_TEXT,
    UNAUTHORIZED_CHANNEL_TEXT,
    UNKNOWN_TEXT,
    operator_reply,
)
from projectos.slack_resolver import (
    format_projects_list,
    format_use_confirmation,
    lookup_project_identifier,
    resolve_slack_project,
    set_session_project,
)
from projectos.slack_slash import (
    SOCKET_COMMANDS,
    parse_conversational_text,
    parse_slash_text,
    project_override_attempt,
)
from projectos.slack_runtime import bootstrap_slack_credentials, prepare_slack_socket_startup
from projectos.slack_state import write_slack_state
from projectos.slack_tokens import app_token, bot_token, contains_secret, reload_slack_tokens
from projectos.store import claim_slack_envelope, claim_slack_events, list_slack_interface_channels, release_slack_events

SLACK_API = "https://slack.com/api"
CONNECTIONS_OPEN = f"{SLACK_API}/apps.connections.open"
AUTH_TEST = f"{SLACK_API}/auth.test"
CHAT_POST = f"{SLACK_API}/chat.postMessage"

OPERATOR_VERBS = {
    "",
    "help",
    "status",
    "summary",
    "work",
    "quality",
    "qa",
    "releases",
    "release",
    "projects",
    "use",
}

HttpPost = Callable[[str, dict[str, str], dict[str, Any] | None], dict[str, Any]]
_MENTION_RE = re.compile(r"<@[^>]+>")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def default_http_post(url: str, headers: dict[str, str], body: dict[str, Any] | None) -> dict[str, Any]:
    payload = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid_json"}
    return data


def ack_envelope(envelope_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"envelope_id": envelope_id}
    if payload is not None:
        message["payload"] = payload
    return message


def _safe_detail(message: str) -> str:
    text = str(message or "")
    if contains_secret(text):
        return "Slack error (details redacted)"
    return text[:240]


def open_socket_url(*, http_post: HttpPost | None = None) -> str:
    token = app_token()
    if not token:
        raise OrchestrationError("PROJECTOS_SLACK_APP_TOKEN is missing")
    post = http_post or default_http_post
    data = post(CONNECTIONS_OPEN, _headers(token), {})
    if not data.get("ok") or not data.get("url"):
        raise OrchestrationError(str(data.get("error") or "apps.connections.open failed"))
    return str(data["url"])


def auth_identity(*, http_post: HttpPost | None = None) -> dict[str, Any]:
    token = bot_token()
    if not token:
        raise OrchestrationError("PROJECTOS_SLACK_BOT_TOKEN is missing")
    post = http_post or default_http_post
    data = post(AUTH_TEST, _headers(token), {})
    if not data.get("ok"):
        raise OrchestrationError(str(data.get("error") or "auth.test failed"))
    return {
        "workspace_name": data.get("team"),
        "team_id": data.get("team_id"),
        "user_id": data.get("user_id"),
    }


def post_message(
    *,
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
    response_url: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any]:
    post = http_post or default_http_post
    if response_url:
        body: dict[str, Any] = {"text": text}
        if blocks:
            body["blocks"] = blocks
        return post(
            response_url,
            {"Content-Type": "application/json; charset=utf-8"},
            body,
        )
    token = bot_token()
    if not token:
        raise OrchestrationError("PROJECTOS_SLACK_BOT_TOKEN is missing")
    body = {"channel": channel_id, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    if blocks:
        body["blocks"] = blocks
    return post(CHAT_POST, _headers(token), body)


def _map_operator_verb(verb: str) -> str:
    mapped = verb
    if verb in {"", "status"}:
        mapped = "summary"
    if verb == "qa":
        mapped = "quality"
    if verb == "release":
        mapped = "releases"
    return mapped


def _project_name(ctx: ServiceContext, project_human_id: str) -> str | None:
    registry = load_registry_or_empty(ctx.registry_path)
    entry = registry.get(project_human_id)
    if entry is None:
        return None
    try:
        return load_repository_identity(entry.repository_root).project_name
    except Exception:
        return None


def handle_projectos_request(
    ctx: ServiceContext,
    *,
    text: str,
    channel_id: str,
    team_id: str | None,
    thread_ts: str | None,
    thread_root_ts: str | None = None,
    user_id: str | None,
    parse_fn=parse_slash_text,
) -> dict[str, Any]:
    if project_override_attempt(text):
        return {
            "text": "Use `/projectos use PRJ-003` or `/projectos PRJ-003 status` to select a project.",
            "response_type": "ephemeral",
        }
    parsed = parse_fn(text)
    verb = str(parsed.get("command") or "summary").lower()
    explicit = parsed.get("project_human_id")
    initialize_database(ctx.db_path)
    session_thread_ts = str(thread_root_ts or thread_ts or "").strip()

    if verb == "help":
        return {"text": HELP_TEXT, "response_type": "ephemeral"}

    if verb == "projects":
        registry = load_registry_or_empty(ctx.registry_path)
        return {"text": format_projects_list(registry), "response_type": "ephemeral"}

    with connection(ctx.db_path) as conn:
        if verb == "use":
            if not explicit:
                resolved = resolve_slack_project(
                    conn,
                    registry_path=ctx.registry_path,
                    channel_id=channel_id,
                    team_id=team_id,
                    thread_ts=session_thread_ts,
                    user_id=user_id,
                    explicit_project=None,
                    require_channel_auth=True,
                )
                if resolved.clarify:
                    return {"text": resolved.clarify_text or HELP_TEXT, "response_type": "ephemeral"}
                if resolved.unauthorized:
                    return {
                        "text": resolved.unauthorized_text or UNAUTHORIZED_CHANNEL_TEXT,
                        "response_type": "ephemeral",
                    }
                return {
                    "text": "Usage: `/projectos use PRJ-003`",
                    "response_type": "ephemeral",
                }
            registry = load_registry_or_empty(ctx.registry_path)
            project_id, error = lookup_project_identifier(registry, str(explicit))
            if error or not project_id:
                return {"text": error or "Unknown project.", "response_type": "ephemeral"}
            auth = resolve_slack_project(
                conn,
                registry_path=ctx.registry_path,
                channel_id=channel_id,
                team_id=team_id,
                thread_ts=session_thread_ts,
                user_id=user_id,
                explicit_project=None,
                require_channel_auth=True,
            )
            if auth.unauthorized:
                return {
                    "text": auth.unauthorized_text or UNAUTHORIZED_CHANNEL_TEXT,
                    "response_type": "ephemeral",
                }
            if not user_id:
                return {
                    "text": "Could not identify your Slack user for session context.",
                    "response_type": "ephemeral",
                }
            set_session_project(
                conn,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=session_thread_ts,
                user_id=user_id,
                project_human_id=project_id,
            )
            return {
                "text": format_use_confirmation(
                    project_id,
                    project_name=_project_name(ctx, project_id),
                ),
                "response_type": "ephemeral",
            }

        resolved = resolve_slack_project(
            conn,
            registry_path=ctx.registry_path,
            channel_id=channel_id,
            team_id=team_id,
            thread_ts=session_thread_ts,
            user_id=user_id,
            explicit_project=str(explicit) if explicit else None,
            require_channel_auth=True,
        )
        if resolved.unauthorized:
            return {
                "text": resolved.unauthorized_text or UNAUTHORIZED_CHANNEL_TEXT,
                "response_type": "ephemeral",
            }
        if resolved.unknown_project:
            return {"text": resolved.unknown_text or "Unknown project.", "response_type": "ephemeral"}
        if resolved.clarify:
            return {"text": resolved.clarify_text or HELP_TEXT, "response_type": "ephemeral"}
        if not resolved.ok or not resolved.project_human_id:
            return {"text": HELP_TEXT, "response_type": "ephemeral"}

        project = resolved.project_human_id
        mapped = _map_operator_verb(verb)
        if verb not in SOCKET_COMMANDS and verb not in OPERATOR_VERBS:
            return {"text": UNKNOWN_TEXT, "response_type": "ephemeral"}
        if mapped in {"help", "summary", "work", "quality", "releases"}:
            return {
                "text": operator_reply(ctx, command=mapped, project_human_id=project, raw_text=""),
                "response_type": "in_channel",
            }
        try:
            result = run_command(
                ctx,
                command=verb,
                channel_id=channel_id,
                team_id=team_id,
                thread_ts=thread_ts,
                project_human_id=project,
                title=parsed.get("title"),
                description=parsed.get("title"),
                source="slack",
            )
        except OrchestrationError as exc:
            return {"text": str(exc), "response_type": "ephemeral"}
        return {
            "text": operator_reply(
                ctx,
                command=verb,
                project_human_id=project,
                raw_text=str(result.get("text") or ""),
            ),
            "response_type": "in_channel",
        }


def handle_slash_commands_payload(ctx: ServiceContext, payload: dict[str, Any]) -> dict[str, Any]:
    command = str(payload.get("command") or "").strip()
    if command and command != "/projectos":
        return {"text": UNKNOWN_TEXT, "response_type": "ephemeral"}
    return handle_projectos_request(
        ctx,
        text=str(payload.get("text") or ""),
        channel_id=str(payload.get("channel_id") or "").strip(),
        team_id=str(payload.get("team_id") or "").strip() or None,
        thread_ts=str(payload.get("thread_ts") or "").strip() or None,
        user_id=str(payload.get("user_id") or "").strip() or None,
        parse_fn=parse_slash_text,
    )


def _event_is_bot_message(event: dict[str, Any], *, bot_user_id: str | None = None) -> bool:
    text = str(event.get("text") or "").strip()
    if text.startswith("*ChatGPT Advisor:*") or text.startswith("*ProjectOS:*"):
        return True
    if event.get("bot_id"):
        return True
    subtype = str(event.get("subtype") or "")
    if subtype in {"bot_message", "message_changed", "message_deleted"}:
        return True
    if bot_user_id and str(event.get("user") or "") == bot_user_id:
        return True
    from projectos.slack_chatgpt_config import chatgpt_slack_user_id

    chatgpt_uid = chatgpt_slack_user_id()
    if chatgpt_uid and str(event.get("user") or "").upper() == chatgpt_uid.upper():
        return True
    return False


def handle_events_api_payload(
    ctx: ServiceContext,
    payload: dict[str, Any],
    *,
    bot_user_id: str | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any] | None:
    if str(payload.get("type") or "") == "url_verification":
        return None
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if _event_is_bot_message(event, bot_user_id=bot_user_id):
        return None
    text = str(event.get("text") or "").strip()
    if not text:
        return None
    channel_id = str(event.get("channel") or "").strip()
    if not channel_id:
        return None
    team_id = str(payload.get("team_id") or "").strip() or ""
    thread_root = thread_root_ts(event)
    dedup_keys = slack_event_dedup_keys(payload, event)
    claimed_keys: list[str] = []
    if dedup_keys:
        initialize_database(ctx.db_path)
        with connection(ctx.db_path) as conn:
            if not claim_slack_events(
                conn,
                dedup_keys,
                team_id=team_id,
                channel_id=channel_id,
                message_ts=str(event.get("ts") or "").strip(),
                event_id=str(payload.get("event_id") or "").strip(),
            ):
                return None
            claimed_keys = list(dedup_keys)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        interface_channels = {
            str(row["channel_id"]) for row in list_slack_interface_channels(conn)
        }
        projectos_active = is_projectos_thread_active(
            conn, team_id=team_id, channel_id=channel_id, thread_ts=thread_root
        )
        chatgpt_thread = None
        from projectos.chatgpt_store import get_chatgpt_thread

        chatgpt_thread = get_chatgpt_thread(
            conn, team_id=team_id, channel_id=channel_id, thread_ts=thread_root
        )
    chatgpt_active = bool(chatgpt_thread and chatgpt_thread.get("active"))

    chatgpt_reply = try_handle_chatgpt_event(
        ctx,
        event=event,
        payload=payload,
        bot_user_id=bot_user_id,
        http_post=http_post,
        projectos_thread_active=projectos_active,
        registered_channel_ids=interface_channels,
    )
    if chatgpt_reply is not None:
        return chatgpt_reply

    if not should_route_projectos_thread_followup(
        event,
        projectos_thread_active=projectos_active,
        chatgpt_thread_active=chatgpt_active,
        text=text,
    ):
        if claimed_keys:
            with connection(ctx.db_path) as conn:
                release_slack_events(conn, claimed_keys)
        return None

    if is_app_mention_event(event):
        text = _MENTION_RE.sub("", text).strip()

    thread_ts = thread_ts_for_event(event)
    reply = handle_projectos_request(
        ctx,
        text=text,
        channel_id=channel_id,
        team_id=team_id or None,
        thread_ts=thread_ts,
        thread_root_ts=thread_root,
        user_id=str(event.get("user") or "").strip() or None,
        parse_fn=parse_conversational_text,
    )
    if reply is not None and thread_root:
        with connection(ctx.db_path) as conn:
            mark_projectos_thread_active(
                conn,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_root,
            )
    if reply is None and claimed_keys:
        with connection(ctx.db_path) as conn:
            release_slack_events(conn, claimed_keys)
    return reply


def process_socket_envelope(
    ctx: ServiceContext,
    envelope: dict[str, Any],
    *,
    http_post: HttpPost | None = None,
    bot_user_id: str | None = None,
) -> dict[str, Any]:
    envelope_id = str(envelope.get("envelope_id") or "").strip()
    kind = str(envelope.get("type") or "").strip()
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    initialize_database(ctx.db_path)
    duplicate = False
    if envelope_id:
        with connection(ctx.db_path) as conn:
            if not claim_slack_envelope(conn, envelope_id, kind):
                duplicate = True
    ack = ack_envelope(envelope_id or "missing")
    if duplicate:
        return {"ack": ack, "duplicate": True, "reply": None}
    reply: dict[str, Any] | None = None
    channel_id = ""
    thread_ts: str | None = None
    response_url: str | None = None
    if kind == "slash_commands":
        reply = handle_slash_commands_payload(ctx, payload)
        channel_id = str(payload.get("channel_id") or "")
        thread_ts = str(payload.get("thread_ts") or "").strip() or None
        response_url = str(payload.get("response_url") or "").strip() or None
    elif kind == "events_api":
        reply = handle_events_api_payload(ctx, payload, bot_user_id=bot_user_id, http_post=http_post)
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        channel_id = str(event.get("channel") or "")
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip() or None
    if reply is None:
        return {"ack": ack, "duplicate": False, "reply": None}
    try:
        post_message(
            channel_id=channel_id,
            text=str(reply.get("text") or ""),
            thread_ts=thread_ts,
            response_url=response_url,
            blocks=reply.get("blocks") if isinstance(reply.get("blocks"), list) else None,
            http_post=http_post,
        )
    except OrchestrationError:
        ack = ack_envelope(envelope_id or "missing", payload={"text": reply.get("text")})
    return {"ack": ack, "duplicate": False, "reply": reply}


def run_socket_mode(
    ctx: ServiceContext,
    *,
    http_post: HttpPost | None = None,
    recv_messages: list[dict[str, Any]] | None = None,
    send_fn: Callable[[dict[str, Any]], None] | None = None,
    max_envelopes: int | None = None,
    require_tokens: bool = True,
) -> int:
    initialize_database(ctx.db_path)
    creds = prepare_slack_socket_startup(enabled=True)
    if require_tokens and not creds.get("tokens_ready"):
        return 0
    bot_user_id: str | None = None
    try:
        identity = auth_identity(http_post=http_post) if bot_token() else {}
        bot_user_id = str(identity.get("user_id") or "") or None
        write_slack_state(
            {
                "status": "connecting",
                "workspace_name": identity.get("workspace_name"),
                "team_id": identity.get("team_id"),
                "detail": "Authenticating to Slack",
            }
        )
        if recv_messages is None:
            url = open_socket_url(http_post=http_post)
            return _run_websocket(ctx, url, http_post=http_post, max_envelopes=max_envelopes, bot_user_id=bot_user_id)
        write_slack_state(
            {
                "status": "connected",
                "detail": "Socket Mode connected",
                "workspace_name": identity.get("workspace_name"),
                "team_id": identity.get("team_id"),
            }
        )
        count = 0
        for envelope in recv_messages:
            result = process_socket_envelope(ctx, envelope, http_post=http_post, bot_user_id=bot_user_id)
            if send_fn:
                send_fn(result["ack"])
            count += 1
            if max_envelopes is not None and count >= max_envelopes:
                break
        return 0
    except OrchestrationError as exc:
        write_slack_state({"status": "error", "detail": _safe_detail(str(exc))})
        return 1
    except Exception as exc:  # noqa: BLE001
        write_slack_state({"status": "error", "detail": _safe_detail(str(exc))})
        return 1


def _run_websocket(
    ctx: ServiceContext,
    url: str,
    *,
    http_post: HttpPost | None,
    max_envelopes: int | None,
    bot_user_id: str | None,
) -> int:
    try:
        from websocket import WebSocketApp
    except ImportError:
        write_slack_state(
            {
                "status": "error",
                "detail": "websocket-client is required for Socket Mode (pip install websocket-client)",
            }
        )
        return 1
    processed = {"n": 0}

    def on_open(_ws):
        write_slack_state({"status": "connected", "detail": "Socket Mode connected"})

    def on_message(ws, message: str):
        try:
            envelope = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(envelope, dict):
            return
        result = process_socket_envelope(ctx, envelope, http_post=http_post, bot_user_id=bot_user_id)
        try:
            ws.send(json.dumps(result["ack"]))
        except Exception:
            pass
        processed["n"] += 1
        if max_envelopes is not None and processed["n"] >= max_envelopes:
            ws.close()

    def on_error(_ws, error):
        write_slack_state({"status": "error", "detail": _safe_detail(str(error))})

    def on_close(_ws, _code, _reason):
        write_slack_state({"status": "disconnected", "detail": "Socket Mode disconnected"})

    app = WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    app.run_forever(ping_interval=30, ping_timeout=10)
    return 0
