"""Operator-facing labels keep canonical IDs intact."""

from projectos.presentation import (
    activity_sentence,
    health_label,
    next_step_sentence,
    queue_label,
    role_label,
    status_label,
)


def test_status_and_role_labels_are_operator_readable() -> None:
    assert status_label("RUNNING") == "In progress"
    assert status_label("SUCCEEDED") == "Finished"
    assert queue_label("ASSURANCE_INTEGRATION") == "Integration review"
    assert queue_label("ASSURANCE_SECURITY") == "Security review"
    assert role_label("ASSURANCE_QUALITY") == "Quality reviewer"
    assert health_label("healthy") == "Healthy"


def test_active_work_and_next_step_sentences() -> None:
    sentence = activity_sentence(
        queue="ASSURANCE_INTEGRATION",
        work_item_human_id="WI-12",
        status="RUNNING",
    )
    assert "Integration review" in sentence
    assert "WI-12" in sentence
    assert "in progress" in sentence.lower()
    integration = activity_sentence(queue="INTEGRATION", work_item_human_id=None, status="RUNNING")
    assert "Combining the approved implementation" in integration
    assert next_step_sentence("INTEGRATION").lower().startswith("release")
    assert "independent reviews" in next_step_sentence("ASSURANCE_SECURITY").lower() or "release" in next_step_sentence("ASSURANCE_SECURITY").lower()
