"""Global settings routes. Integration secrets use encrypted local storage."""

from __future__ import annotations

from fastapi import APIRouter

from projectos.http.schemas import (
    OpenAISecretDeleteResponse,
    OpenAISecretPutRequest,
    OpenAISecretPutResponse,
    OpenAISettingsResponse,
    OpenAISettingsUpdateRequest,
    OpenAITestResponse,
    SlackSettingsResponse,
    SlackSettingsUpdateRequest,
    SlackTestResponse,
    SlackTokensUpdateRequest,
    SlackTokensUpdateResponse,
)
from projectos.openai_secret_setup import apply_openai_secret, remove_openai_secret, test_openai_connection
from projectos.openai_settings import read_openai_settings, update_openai_settings
from projectos.slack_settings import read_slack_settings, update_slack_settings
from projectos.slack_token_setup import apply_slack_tokens, probe_slack_connection

router = APIRouter(prefix="/v1/settings", tags=["settings"])


@router.get("/integrations/slack", response_model=SlackSettingsResponse)
def get_slack_settings() -> SlackSettingsResponse:
    return SlackSettingsResponse.model_validate(read_slack_settings())


@router.put("/integrations/slack", response_model=SlackSettingsResponse)
def put_slack_settings(body: SlackSettingsUpdateRequest) -> SlackSettingsResponse:
    payload = body.model_dump(exclude_none=True)
    return SlackSettingsResponse.model_validate(update_slack_settings(payload))


@router.post("/integrations/slack/tokens", response_model=SlackTokensUpdateResponse)
def post_slack_tokens(body: SlackTokensUpdateRequest) -> SlackTokensUpdateResponse:
    payload = body.model_dump(exclude_none=True)
    return SlackTokensUpdateResponse.model_validate(
        apply_slack_tokens(
            app_token=payload.get("app_token"),
            bot_token=payload.get("bot_token"),
            signing_secret=payload.get("signing_secret"),
        )
    )


@router.post("/integrations/slack/test", response_model=SlackTestResponse)
def post_slack_test() -> SlackTestResponse:
    return SlackTestResponse.model_validate(probe_slack_connection())


@router.get("/integrations/openai", response_model=OpenAISettingsResponse)
def get_openai_settings() -> OpenAISettingsResponse:
    return OpenAISettingsResponse.model_validate(read_openai_settings())


@router.put("/integrations/openai", response_model=OpenAISettingsResponse)
def put_openai_settings(body: OpenAISettingsUpdateRequest) -> OpenAISettingsResponse:
    payload = body.model_dump(exclude_none=True)
    return OpenAISettingsResponse.model_validate(update_openai_settings(payload))


@router.put("/integrations/openai/secret", response_model=OpenAISecretPutResponse)
def put_openai_secret(body: OpenAISecretPutRequest) -> OpenAISecretPutResponse:
    payload = body.model_dump(exclude_none=True)
    api_key = payload.get("api_key")
    if api_key is None or not str(api_key).strip():
        from projectos.errors import OrchestrationError

        raise OrchestrationError("OpenAI API key is required")
    return OpenAISecretPutResponse.model_validate(apply_openai_secret(api_key_value=api_key))


@router.delete("/integrations/openai/secret", response_model=OpenAISecretDeleteResponse)
def delete_openai_secret() -> OpenAISecretDeleteResponse:
    return OpenAISecretDeleteResponse.model_validate(remove_openai_secret())


@router.post("/integrations/openai/test", response_model=OpenAITestResponse)
def post_openai_test() -> OpenAITestResponse:
    return OpenAITestResponse.model_validate(test_openai_connection())
