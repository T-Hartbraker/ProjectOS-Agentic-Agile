"""Slack event routing helpers for Socket Mode (public, private, DM)."""

from __future__ import annotations

from typing import Any

# Private #projectos channels use channel_type "group" and message.groups events.
PRIVATE_CHANNEL_TYPE = "group"
PUBLIC_CHANNEL_TYPE = "channel"
DM_CHANNEL_TYPE = "im"
PRIVATE_CHANNEL_PREFIX = "G"
PUBLIC_CHANNEL_PREFIX = "C"
DM_CHANNEL_PREFIX = "D"


def event_channel_type(event: dict[str, Any]) -> str:
    return str(event.get("channel_type") or "").strip()


def channel_id_for_event(event: dict[str, Any]) -> str:
    return str(event.get("channel") or "").strip()


def infer_channel_type(event: dict[str, Any]) -> str:
    explicit = event_channel_type(event)
    if explicit:
        return explicit
    channel_id = channel_id_for_event(event).upper()
    if channel_id.startswith(PRIVATE_CHANNEL_PREFIX):
        return PRIVATE_CHANNEL_TYPE
    if channel_id.startswith(DM_CHANNEL_PREFIX):
        return DM_CHANNEL_TYPE
    if channel_id.startswith(PUBLIC_CHANNEL_PREFIX):
        return PUBLIC_CHANNEL_TYPE
    return ""


def is_dm_message_event(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == "message" and infer_channel_type(event) == DM_CHANNEL_TYPE


def is_private_channel_message_event(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == "message" and infer_channel_type(event) == PRIVATE_CHANNEL_TYPE


def is_public_channel_message_event(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == "message" and infer_channel_type(event) == PUBLIC_CHANNEL_TYPE


def is_app_mention_event(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == "app_mention"


def is_thread_reply_event(event: dict[str, Any]) -> bool:
    return bool(str(event.get("thread_ts") or "").strip())


def is_interface_channel_message_event(event: dict[str, Any]) -> bool:
    """Messages delivered for the global interface channel (private group or DM)."""
    if is_app_mention_event(event):
        return True
    if is_dm_message_event(event):
        return True
    if is_private_channel_message_event(event):
        return True
    return False


def is_registered_interface_channel_event(
    event: dict[str, Any],
    *,
    registered_channel_ids: set[str] | frozenset[str] | None = None,
) -> bool:
    """True when the event is routable in a configured ProjectOS interface channel."""
    if is_interface_channel_message_event(event):
        return True
    channel_id = channel_id_for_event(event)
    if not channel_id or not registered_channel_ids:
        return False
    if str(event.get("type") or "") != "message":
        return False
    return channel_id in registered_channel_ids


def should_route_projectos_thread_followup(
    event: dict[str, Any],
    *,
    projectos_thread_active: bool,
    chatgpt_thread_active: bool,
    text: str,
) -> bool:
    if is_app_mention_event(event):
        return True
    if is_dm_message_event(event):
        return True
    if not is_thread_reply_event(event):
        return False
    if not (is_private_channel_message_event(event) or is_dm_message_event(event)):
        return False
    from projectos.slack_chatgpt import is_chatgpt_addressed

    if is_chatgpt_addressed(text, event=event):
        return False
    if chatgpt_thread_active and not projectos_thread_active:
        return False
    return projectos_thread_active


def thread_ts_for_event(event: dict[str, Any]) -> str | None:
    return str(event.get("thread_ts") or event.get("ts") or "").strip() or None


def message_ts_for_event(event: dict[str, Any]) -> str | None:
    return str(event.get("ts") or "").strip() or None
