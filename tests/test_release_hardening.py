"""Release versioning and installer placeholder tests."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.delivery.release_version import read_declared_version, resolve_release_version
from projectos.delivery.store import insert_delivery_release, update_delivery_release
from projectos.migrate import initialize_database
from projectos.packaging.python_desktop import PythonDesktopAdapter
from projectos.delivery.contract import DeliveryContract
from projectos.services.context import ServiceContext


def _ctx(tmp_path: Path) -> tuple[ServiceContext, Path]:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    (repo / "pyproject.toml").write_text('[project]\nname = "gamma"\nversion = "2.1.0"\n', encoding="utf-8")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json"), repo


def test_read_declared_version_from_pyproject(tmp_path: Path) -> None:
    _, repo = _ctx(tmp_path)
    assert read_declared_version(repo) == "2.1.0"


def test_resolve_release_version_bumps_published_collision(tmp_path: Path) -> None:
    ctx, repo = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        insert_delivery_release(
            conn,
            release_record_id="REL-1",
            project_human_id="PRJ-003",
            release_human_id="REL-H-1",
            version="2.1.0",
            candidate_git_sha="abc",
            lifecycle_status="released",
        )
        update_delivery_release(conn, "REL-1", publication_status="published")
        version = resolve_release_version(conn, project_id="PRJ-003", repo_root=repo)
    assert version == "2.1.1"


def test_python_desktop_placeholder_has_honest_extension(tmp_path: Path) -> None:
    _, repo = _ctx(tmp_path)
    contract = DeliveryContract(
        schema_version=1,
        delivery_type="desktop_application",
        target_platforms=("windows-x64",),
        packaging_adapter="python_desktop",
        repository_provider="github",
        repository_owner="acme",
        repository_name="gamma",
        default_branch="main",
        release_strategy="semantic_version",
        installer_format="exe",
        installer_name_template="{product}-Setup-{version}.exe",
        artifact_retention=10,
        code_signing_policy="not_required",
        sbom_policy="required",
        checksum_policy="sha256",
        github_release_enabled=False,
        slack_release_announcement_enabled=False,
        product_name="Gamma",
    )
    adapter = PythonDesktopAdapter()
    build_dir = tmp_path / "build"
    output_dir = tmp_path / "out"
    result = adapter.package(
        repo, contract, version="1.0.0", git_sha="sha", build_dir=build_dir, output_dir=output_dir
    )
    installer = result.artifacts[0]
    assert installer.artifact_name.endswith("-installer-placeholder.json")
    assert not installer.artifact_name.endswith(".exe")
    payload = json.loads(installer.local_path.read_text(encoding="utf-8"))
    assert payload["placeholder"] is True
