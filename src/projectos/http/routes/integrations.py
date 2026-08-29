"""External integrations. Slack identifiers are metadata, not project state."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from projectos.http.deps import get_cursor_runner, get_projectctl_runner, get_slack_service
from projectos.http.schemas import (
    SlackCommandRequest,
    SlackCommandResponse,
    SlackInboundRequest,
    SlackInboundResponse,
    SlackStatusResponse,
)
from projectos.services import SlackBindingService
from projectos.slack_slash import handle_slash_form, parse_form, verify_slack_request

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])


@router.get("/slack/status", response_model=SlackStatusResponse)
def slack_status() -> SlackStatusResponse:
    from projectos.slack_status import public_slack_status

    return SlackStatusResponse.model_validate(public_slack_status())



@router.post("/slack/inbound", response_model=SlackInboundResponse)
def slack_inbound(
    body: SlackInboundRequest,
    svc: SlackBindingService = Depends(get_slack_service),
    cursor_runner=Depends(get_cursor_runner),
    projectctl_runner=Depends(get_projectctl_runner),
) -> SlackInboundResponse:
    work = body.work_request.model_dump() if body.work_request is not None else None
    return SlackInboundResponse.model_validate(
        svc.inbound(
            channel_id=body.channel_id,
            team_id=body.team_id,
            thread_ts=body.thread_ts,
            message_ts=body.message_ts,
            project_human_id=body.project_human_id,
            work_request=work,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
        )
    )


@router.post("/slack/slash")
async def slack_slash(
    request: Request,
    svc: SlackBindingService = Depends(get_slack_service),
) -> JSONResponse:
    raw = await request.body()
    timestamp = request.headers.get("x-slack-request-timestamp")
    signature = request.headers.get("x-slack-signature")
    if not verify_slack_request(
        signing_secret=None,
        timestamp=timestamp,
        signature=signature,
        body=raw,
    ):
        return JSONResponse({"response_type": "ephemeral", "text": "Slack signature rejected."}, status_code=401)
    form = parse_form(raw)
    payload = handle_slash_form(form, command_fn=svc.command)
    return JSONResponse(payload)


@router.post("/slack/command", response_model=SlackCommandResponse)
def slack_command(
    body: SlackCommandRequest,
    svc: SlackBindingService = Depends(get_slack_service),
) -> SlackCommandResponse:
    return SlackCommandResponse.model_validate(
        svc.command(
            command=body.command,
            channel_id=body.channel_id,
            team_id=body.team_id,
            thread_ts=body.thread_ts,
            message_ts=body.message_ts,
            project_human_id=body.project_human_id,
            title=body.title,
            description=body.description,
            source=body.source,
        )
    )

