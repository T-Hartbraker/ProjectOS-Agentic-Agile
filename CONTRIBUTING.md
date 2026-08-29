# Contributing

## Principles

1. **PM outcome authority** — downstream agent failures are evidence, not terminal policy.
2. **Closed-loop remediation** — replace direct `RUN_BLOCKED` on recoverable QA/delivery failures with PM-owned remediation.
3. **Minimal diffs** — match existing conventions; avoid unrelated refactors.
4. **Auditable history** — one logical change per commit with targeted tests passing.

## Getting started

```bash
pip install -e ".[http]"
cp config/projects.example.json config/projects.json
py -m pytest
cd web && npm install && npm test && npm run build
```

## Commit discipline

- Use conventional commit prefixes: `feat`, `fix`, `refactor`, `test`, `chore`, `docs`.
- Run targeted tests for the area you changed before committing.
- Do not commit `state/`, `logs/`, `config/projects.json`, or secrets.

## Testing expectations

- Backend: `py -m pytest`
- Dashboard: `cd web && npm test && npm run build`
- New orchestration behavior requires regression tests asserting PM contracts, not obsolete terminal-downstream semantics.

## Architecture references

- Run outcomes: `src/projectos/run_outcomes.py`
- PM remediation: `src/projectos/pm_remediation.py`, `src/projectos/pm_delivery_remediation.py`
- Store decomposition note: `docs/design/store-decomposition-todo.md`
