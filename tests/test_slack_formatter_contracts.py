"""Contract tests for Sponsor-facing format_* functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.services.context import ServiceContext
from projectos.slack_replies import (
    format_help,
    format_quality,
    format_releases,
    format_summary,
    format_work,
)


def _ctx(tmp_path: Path) -> ServiceContext:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    return ServiceContext(db_path=tmp_path / "projectos.db", registry_path=tmp_path / "projects.json")


@pytest.mark.parametrize(
    "formatter,kwargs",
    [
        (format_summary, {}),
        (format_work, {}),
        (format_quality, {}),
        (format_releases, {}),
        (format_releases, {"raw_text": "release check"}),
    ],
)
def test_formatters_accept_empty_project_state(tmp_path: Path, formatter, kwargs) -> None:
    ctx = _ctx(tmp_path)
    text = formatter(ctx, "PRJ-003", **kwargs)
    assert "PRJ-003" in text
    assert isinstance(text, str)


def test_format_help_contract() -> None:
    text = format_help()
    assert "/projectos" in text
    assert isinstance(text, str)


def test_format_releases_keyword_only_raw_text(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(TypeError):
        format_releases(ctx, "PRJ-003", "positional raw text")  # type: ignore[misc]


def test_sponsor_query_release_path_no_formatter_exception(tmp_path: Path) -> None:
    from projectos.migrate import initialize_database
    from projectos.sponsor_query import SponsorQueryService

    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    text = SponsorQueryService(ctx).get_release_summary(
        "PRJ-003",
        raw_text="I want ProjectOS to re-release the package",
    )
    assert isinstance(text, str)
    assert "PRJ-003" in text
