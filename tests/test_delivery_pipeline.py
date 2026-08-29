"""Comprehensive delivery pipeline tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.delivery.contract import infer_delivery_contract, load_delivery_contract
from projectos.delivery.gates import GATE_STATUS_FAILED, GATE_STATUS_PASSED
from projectos.delivery.manifest import build_release_manifest, validate_release_manifest
from projectos.delivery.semver import format_tag, parse_semver, propose_bump
from projectos.delivery.service import DeliveryService
from projectos.errors import OrchestrationError
from projectos.github.client import GitHubClient, PublicationResult
from projectos.github.secret_setup import apply_github_token
from projectos.github.tokens import reload_github_tokens, resolve_github_credentials
from projectos.migrate import initialize_database
from projectos.packaging.registry import detect_packaging_adapter
from projectos.services.context import ServiceContext


def _delivery_json(**overrides) -> dict:
    base = infer_delivery_contract(
        product_name="ExampleProduct",
        repository_owner="acme",
        repository_name="example-product",
        target_platforms=["windows-x64"],
        external_distribution=False,
    )
    base.update(overrides)
    return base


def _setup_project(tmp_path: Path) -> ServiceContext:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id="PRJ-004", project_name="Example Product")
    (repo / "pyproject.toml").write_text("[project]\nname='example'\nversion='0.1.0'\n", encoding="utf-8")
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / "project" / "delivery.json").write_text(
        json.dumps(_delivery_json(github_release_enabled=False)),
        encoding="utf-8",
    )
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-004", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def test_delivery_contract_validation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project").mkdir()
    (repo / "project" / "delivery.json").write_text(json.dumps(_delivery_json()), encoding="utf-8")
    contract = load_delivery_contract(repo)
    assert contract.repository_slug == "acme/example-product"


def test_packaging_adapter_detection(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "project").mkdir()
    (repo / "project" / "delivery.json").write_text(
        json.dumps(_delivery_json(packaging_adapter="auto")),
        encoding="utf-8",
    )
    contract = load_delivery_contract(repo)
    assert detect_packaging_adapter(repo, contract) == "python_desktop"


def test_ambiguous_adapter_rejection(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    (repo / "project").mkdir()
    (repo / "project" / "delivery.json").write_text(
        json.dumps(_delivery_json(packaging_adapter="auto")),
        encoding="utf-8",
    )
    contract = load_delivery_contract(repo)
    with pytest.raises(OrchestrationError, match="Ambiguous"):
        detect_packaging_adapter(repo, contract)


def test_untrusted_build_command_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project").mkdir()
    payload = _delivery_json(trusted_build_command="echo ok | tee")
    (repo / "project" / "delivery.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OrchestrationError, match="forbidden"):
        load_delivery_contract(repo)


def test_semver_and_tag() -> None:
    assert parse_semver("1.2.3") == (1, 2, 3, None)
    assert propose_bump("1.2.3", change_type="patch") == "1.2.4"
    assert format_tag("1.0.0") == "v1.0.0"


def test_manifest_validation() -> None:
    manifest = build_release_manifest(
        project_id="PRJ-004",
        release_id="REL-001",
        product_name="Example",
        version="1.0.0",
        git_sha="abc123",
        build_id="BLD-1",
        target_platform="windows-x64",
        artifact_filename="Example-Setup-1.0.0.exe",
        artifact_sha256="deadbeef",
        artifact_size=10,
        signature_status="unsigned",
        sbom_ref="sbom.json",
        release_status="qa_passed",
        build_executor="LOCAL",
        repository="acme/example",
        release_url=None,
    )
    validate_release_manifest(
        manifest,
        expected_project_id="PRJ-004",
        expected_release_id="REL-001",
        expected_version="1.0.0",
        expected_git_sha="abc123",
        expected_artifact_sha256="deadbeef",
    )
    bad = dict(manifest)
    bad["git_sha"] = "other"
    with pytest.raises(OrchestrationError, match="git_sha"):
        validate_release_manifest(
            bad,
            expected_project_id="PRJ-004",
            expected_release_id="REL-001",
            expected_version="1.0.0",
            expected_git_sha="abc123",
            expected_artifact_sha256="deadbeef",
        )


def test_prepare_package_verify_publish_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_GITHUB_TOKEN", "")
    reload_github_tokens()
    ctx = _setup_project(tmp_path)
    svc = DeliveryService(ctx)
    prepared = svc.prepare_release("PRJ-004", release_human_id="REL-001", version="1.0.0")
    record_id = prepared["release_record_id"]
    packaged = svc.package_release(record_id)
    assert packaged["build_id"]
    installer = next(item for item in packaged["artifacts"] if item["artifact_type"] == "installer")
    assert installer["sha256"]
    verified = svc.verify_release(record_id)
    assert verified["manifest_sha256"]
    published = svc.publish_release(record_id)
    assert published["lifecycle_status"] == "released"
    assert published["publication_status"] == "published"


def test_idempotent_prepare_and_publish(tmp_path: Path) -> None:
    ctx = _setup_project(tmp_path)
    svc = DeliveryService(ctx)
    first = svc.prepare_release("PRJ-004", release_human_id="REL-001", version="1.0.0")
    second = svc.prepare_release("PRJ-004", release_human_id="REL-001", version="1.0.0")
    assert first["release_record_id"] == second["release_record_id"]
    record_id = first["release_record_id"]
    svc.package_release(record_id)
    svc.verify_release(record_id)
    pub1 = svc.publish_release(record_id)
    pub2 = svc.publish_release(record_id)
    assert pub1["release_record_id"] == pub2["release_record_id"]


def test_github_publication_mock(tmp_path: Path, monkeypatch) -> None:
    ctx = _setup_project(tmp_path)
    repo = Path(ctx.registry_path).parent / "product"
    delivery = json.loads((repo / "project" / "delivery.json").read_text(encoding="utf-8"))
    delivery["github_release_enabled"] = True
    (repo / "project" / "delivery.json").write_text(json.dumps(delivery), encoding="utf-8")
    monkeypatch.setenv("PROJECTOS_GITHUB_TOKEN", "ghp_testtoken1234567890")
    reload_github_tokens()

    calls: list[str] = []

    def fake_post(url, headers, body=None, method="POST"):
        calls.append(url)
        if url.endswith("/repos/acme/example-product") and method == "GET":
            return {"id": 1, "full_name": "acme/example-product", "default_branch": "main"}
        if url.endswith("/releases") and method == "POST":
            return {
                "id": 2,
                "html_url": "https://github.com/acme/example-product/releases/tag/v1.0.0",
                "upload_url": "https://upload.example/{?name,label}",
            }
        if "upload.example" in url:
            name = url.split("name=")[-1]
            return {"id": 3, "browser_download_url": f"https://github.com/download/{name}"}
        if "/releases/tags/" in url and method == "GET":
            return {"message": "Not Found"}
        return {"ok": True}

    client = GitHubClient(http_post=fake_post, token="ghp_testtoken1234567890")
    svc = DeliveryService(ctx, github_client=client)
    prepared = svc.prepare_release("PRJ-004", release_human_id="REL-001", version="1.0.0")
    record_id = prepared["release_record_id"]
    svc.package_release(record_id)
    svc.verify_release(record_id)
    published = svc.publish_release(record_id)
    assert published["github_release_url"]
    assert "releases" in published["github_release_url"]


def test_duplicate_tag_protection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_GITHUB_TOKEN", "ghp_testtoken1234567890")
    reload_github_tokens()

    def fake_post(url, headers, body=None, method="POST"):
        if "/releases/tags/" in url:
            return {"id": 99, "html_url": "https://github.com/existing", "upload_url": "https://upload/{?name,label}"}
        if url.endswith("/repos/acme/example-product"):
            return {"id": 1, "full_name": "acme/example-product"}
        if url.endswith("/releases"):
            raise AssertionError("create_release should not be called when tag exists")
        if "upload" in url:
            return {"browser_download_url": "https://github.com/asset"}
        return {"ok": True}

    client = GitHubClient(http_post=fake_post, token="ghp_testtoken1234567890")
    result = client.publish_release_assets(
        "acme",
        "example-product",
        tag="v1.0.0",
        title="t",
        body="b",
        target_commitish="abc",
        assets={"a.txt": (b"x", "text/plain")},
    )
    assert result.release_url == "https://github.com/existing"


def test_github_secret_redaction(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("projectos.secret_store._projectos_secrets_file", lambda: tmp_path / "secrets.enc")
    apply_github_token(token_value="ghp_supersecretvalue123456")
    creds = resolve_github_credentials(refresh=True)
    assert creds["configured"]
    from projectos.github.tokens import contains_secret

    assert contains_secret("token ghp_supersecretvalue123456")
    settings = {"configured": creds["configured"]}
    assert "ghp_supersecretvalue123456" not in str(settings)


def test_slack_release_announcement(tmp_path: Path) -> None:
    ctx = _setup_project(tmp_path)
    messages: list[str] = []
    svc = DeliveryService(ctx, slack_poster=messages.append)
    prepared = svc.prepare_release("PRJ-004", release_human_id="REL-001", version="1.0.0")
    record_id = prepared["release_record_id"]
    svc.package_release(record_id)
    svc.verify_release(record_id)
    svc.publish_release(record_id)
    assert messages
    assert "RELEASED" in messages[0]
    assert "SHA-256" in messages[0]
