# ProjectOS

ProjectOS is an agentic control plane for governed software delivery. Sponsors express objectives in Slack; an Advisor translates intent; the **PM Agent** is the sole authority for run outcomes. Specialist agents (Development, QA, Architecture, Security, Delivery, Release) emit evidence and findings. QA failures and recoverable delivery gaps enter **closed-loop remediation** rather than immediate terminal failure.

## Architecture model

```
Sponsor → Advisor → PM Agent → Agents → QA → remediation → Release
```

**Core invariant:** agent failure is evidence; PM outcome is policy.

Only the PM may terminalize an `execution_run` via:

- `RUN_COMPLETED` (success)
- `WAITING_FOR_SPONSOR` (paused, resumable)
- `RUN_BLOCKED` (unrecoverable technical condition)
- `RUN_ESCALATED` (remediation policy exceeded)
- `RUN_CANCELLED` (sponsor cancelled)

Operational events such as `QA_GATE_FAILED`, `PACKAGE_FAILED`, and `DELIVERY_CONTRACT_MISSING` update phase and evidence but do not directly close runs.

## Setup

1. Create a Python 3.11+ virtual environment and install the package:

   ```bash
   pip install -e ".[http]"
   ```

2. Copy the project registry template and register local repositories:

   ```bash
   cp config/projects.example.json config/projects.json
   ```

   Edit `config/projects.json` with absolute paths to your delivery-project repositories.

3. Initialize local runtime state (created on first run):

   - SQLite database: `state/projectos.db`
   - Operator logs: `logs/operator/`
   - Run evidence: `logs/runs/`

4. Optional: configure Slack and OpenAI integrations via operator settings (not committed to Git).

## Development commands

| Command | Purpose |
|---------|---------|
| `py -m pytest` | Backend + orchestration tests |
| `cd web && npm test` | Dashboard unit tests |
| `cd web && npm run build` | Production dashboard build |
| `python -m projectos.http` | Local loopback API (default `127.0.0.1:8787`) |
| `projectos api` | CLI API entry (same bind policy) |

## Integrations

- **Slack:** Sponsor threads, Advisor deliberation, PM activity feed, governed handoffs.
- **OpenAI:** Advisor assistance with fact/inference boundaries (local state under `state/`).

## Delivery contract

Each delivery-project repository should contain `project/delivery.json` defining packaging adapter, publication destination, signing policy, and installer expectations. ProjectOS can infer a governed draft when missing and safe; Sponsor-only decisions (repository owner/name, signing policy, ambiguous platforms) pause the run at `WAITING_FOR_SPONSOR`.

## Installer limitation

There is **no production Windows installer backend** in this repository yet. The `python_desktop` adapter emits an honest `*-installer-placeholder.json` artifact. A finished installer request requires a real executable backend validated separately.

## Security model

See [SECURITY.md](SECURITY.md). Local development defaults to loopback-only unauthenticated API; non-loopback bind without authentication is refused at startup.

## Repository layout

| Path | Purpose |
|------|---------|
| `src/projectos/` | Core control plane, PM orchestration, delivery pipeline |
| `web/` | Sponsor dashboard (React/Vite) |
| `migrations/` | SQLite schema migrations |
| `config/projects.example.json` | Registry template |
| `state/` | Local runtime database and transient state (gitignored) |
| `tests/` | Regression and contract tests |
| `docs/` | Architecture and design notes |

## License

LICENSE STATUS: NOT YET SELECTED
