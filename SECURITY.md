# Security

## Secrets

- **Never commit secrets to Git.** This includes API tokens, Slack signing secrets, GitHub credentials, encryption keys, and `.env` files.
- Use local operator configuration and OS secret storage. Runtime secret material belongs under `state/` (gitignored).

## Local state and databases

- `state/projectos.db` — authoritative local orchestration database
- `state/openai_state.json` — transient OpenAI integration state
- `config/projects.json` — machine-local project registry (use `config/projects.example.json` as template)
- `logs/` — operator and run evidence (local only)

Treat these paths as sensitive on shared machines. Back up or exclude them from public artifacts.

## HTTP API development mode

The HTTP API defaults to:

- bind address: `127.0.0.1` (loopback)
- `auth_required=False` with a broad local actor for development convenience

**Production and enterprise deployments must not** expose an unauthenticated API on non-loopback interfaces. Startup refuses `auth_required=False` when binding beyond loopback.

Enable explicit authentication and actor policies before any network-exposed deployment.

## Reporting vulnerabilities

Security reporting process: **placeholder — contact repository maintainers through your organization's standard channel.**

Do not open public issues for undisclosed security vulnerabilities.
