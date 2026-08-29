# ProjectOS Universal Delivery Architecture

ProjectOS extends the existing governed release lifecycle with a **Delivery Contract** per project, packaging adapters, release gates, GitHub publication, and Slack delivery cards.

## Control plane

ProjectOS remains authoritative. ChatGPT may propose `prepare_release`, `package_release`, or `publish_release`, but execution requires immutable proposal approval.

## Delivery contract

Each delivery repository declares `project/delivery.json` (schema: `schemas/delivery.schema.json`).

Key fields:

- `delivery_type`, `target_platforms`, `packaging_adapter`
- `repository_owner`, `repository_name`, `default_branch`
- `installer_name_template`, `code_signing_policy`, `sbom_policy`
- `github_release_enabled`, `slack_release_announcement_enabled`

Optional `trusted_build_command` is allowlisted (no shell metacharacters). Commands are never accepted from Slack/ChatGPT/model output.

## Packaging adapters

| Adapter | Detection |
|---------|-----------|
| `python_desktop` | `pyproject.toml` or `setup.py` |
| `generic` | explicit contract + `trusted_build_command` |
| `auto` | single matching adapter or fail closed |

Operations: `detect`, `validate_environment`, `build`, `package`, `verify`, `collect_artifacts`.

## Release lifecycle

Existing projectctl lifecycle is preserved:

`planned → candidate → qa_passed → released`

Between `qa_passed` and `released`, ProjectOS enforces release gates:

`QA_GATE`, `SOURCE_GATE`, `BUILD_GATE`, `PACKAGE_GATE`, `CHECKSUM_GATE`, `SBOM_GATE`, `SIGNATURE_GATE`, `PUBLICATION_GATE`, `DELIVERY_GATE`

## Artifact model

Distributable binaries live on disk under `state/delivery/builds/<release_record_id>/` and optionally on GitHub Releases. SQLite stores metadata only (`delivery_artifacts`).

## Build executors

- `LOCAL` — development, diagnostics, FAT
- `CI` — GitHub Actions (`templates/github/projectos-release.yml`)

## GitHub integration

- Token: `github.token` in `state/projectos_secrets.enc` or `PROJECTOS_GITHUB_TOKEN`
- Settings API: metadata only, never token values
- GitHub Releases are the canonical binary distribution location

## CLI

```text
projectos delivery show --project PRJ-###
projectos delivery validate --project PRJ-###

projectos release prepare --project PRJ-### --release REL-001 --version 1.0.0
projectos release package --record DLV-...
projectos release verify --record DLV-...
projectos release publish --record DLV-...
projectos release artifacts --record DLV-...
projectos release manifest --record DLV-...

projectos github doctor
```

## HTTP API

- `GET /v1/projects/{id}/delivery`
- `POST /v1/projects/{id}/delivery/validate`
- `POST /v1/projects/{id}/delivery/releases/prepare`
- `POST /v1/delivery/releases/{record}/package|verify|publish`
- `GET/PUT/DELETE /v1/settings/integrations/github/token`

## Slack

After successful publication, ProjectOS posts a release card with installer link, SHA-256, GitHub Release URL, and SBOM link. Slack is not the system of record for binaries.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `delivery.json missing` | Add contract before prepare |
| `Ambiguous packaging adapters` | Set `packaging_adapter` explicitly |
| `Signature gate pending` | Production GitHub publish requires signing policy resolution |
| `database is locked` | Retry after concurrent operation completes |
