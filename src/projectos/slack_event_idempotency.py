"""Stable Slack event idempotency keys for Socket Mode / Events API."""

from __future__ import annotations

from typing import Any


def slack_event_dedup_keys(payload: dict[str, Any], event: dict[str, Any]) -> list[str]:
    """Build dedup keys for one logical Slack user message delivery.

    Prefer Slack ``event_id`` (stable across Socket Mode retries). Also include
    team + channel + message ``ts`` so separate ``message`` / ``app_mention``
    deliveries for the same user message collapse to one processing cycle.
    """
    keys: list[str] = []
    seen: set[str] = set()

    def _add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    event_id = str(payload.get("event_id") or "").strip()
    if event_id:
        _add(f"event_id:{event_id}")

    team_id = str(payload.get("team_id") or "").strip()
    channel_id = str(event.get("channel") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    if team_id and channel_id and message_ts:
        _add(f"message:{team_id}:{channel_id}:{message_ts}")

    client_msg_id = str(event.get("client_msg_id") or "").strip()
    if team_id and channel_id and client_msg_id:
        _add(f"client:{team_id}:{channel_id}:{client_msg_id}")

    return keys
