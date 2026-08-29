"""OpenAI client helpers."""

from __future__ import annotations

from projectos.openai_client import extract_output_text


def test_extract_output_text_from_responses_payload() -> None:
    data = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello Sponsor."}],
            }
        ]
    }
    assert extract_output_text(data) == "Hello Sponsor."
