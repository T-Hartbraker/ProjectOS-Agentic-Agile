# Phase 3 baseline — ProjectOS as of Phase 2 FAT

This document snapshots the **current** ProjectOS architecture after Phase 2 FAT. It is descriptive, not a design of the future HTTP API. Runtime behavior is unchanged by this note.

Entry point: `python -m projectos` (`projectos.cli:main`).

---

## 1. CLI surface

Global flags (parser root):

| Flag | Default | Meaning |
|------|---------|---------|
| `--config` | `config/projects.json` | Registry path (`DEFAULT_REGISTRY_PATH`) |

Most subcommands also take `--db` (default `state/projectos.db` / `DEFAULT_DB_PATH`).

| Command | Purpose | Application core |
|---------|---------|------------------|
| `registry list` | List registry rows | `load_registry` |
| `registry show <id>` | Show one entry | `load_registry` |
| `registry validate [id]` | Identity + projectctl binding | `validate_registry` |
| `plan --project … [--iteration] [--dry-run]` | Durable job graph from PM plan | `run_plan` |
| `worker [--once] [--queue] [--role] [--job]` | Execute one READY job | `run_once` |
| `dispatch --once \| --until-idle [--max-parallel]` | Bounded parallel READY dispatch | `run_dispatch` |
| `recover [--no-promote]` | Expired leases, identity, worktrees | `run_recovery` |
| `recover --salvage-candidate --job` | Salvage FAILED delivery HEAD | `salvage_delivery_candidate` |
| `recover --reconcile-release --job` | Successor RELEASE on integration SHA | `reconcile_stale_release` |
| `recover --retry-release` | Alias of `--reconcile-release` | same |
| `recover --revalidate-blocked --job` | BLOCKED → READY if work-item now valid | `revalidate_blocked_job` |
| `recover --reclaim-running --job` | Interrupted RUNNING/LEASED reclaim | `reclaim_interrupted_running_job` |
| `cursor smoke --workspace` | Headless Cursor adapter diagnostic | `run_cursor_smoke_test` |
| `budget --project [--iteration]` | Token/run accounting | `build_budget_report` |
| `iteration run --project [--iteration]` | Checkpointed conductor | `run_iteration` |
| `schedule show \| due \| set` | Per-project cadence | `list_schedules`, `evaluate_due`, `upsert_schedule` |
| `daemon run \| status \| stop` | Long-running loop | `run_daemon`, `get_daemon_status`, `stop_daemon` |
| `doctor` | Deterministic health check | `run_doctor` |
| `fat reconcile --project --iteration [--skip-work-items]` | PRJ-003/ITER-002 no-op invalidation | `reconcile_prj003_iter002_fat` |

CLI currently **formats** dataclass results as stdout lines and maps `ProjectOSError` (and unexpected exceptions) to exit code 1. It does not own orchestration policy.

---

## 2. Registry model

**File:** `config/projects.json`  
**Schema:** `schemas/projects.schema.json` (`schema_version` = 1)

Each entry:

| Field | Role |
|-------|------|
| `project_human_id` | Stable ProjectOS + projectctl identity (e.g. `PRJ-003`) |
| `repository_root` | Absolute path to a **delivery-project** git root |
| `enabled` | Disabled entries are skipped by validate/dispatch consumers |

Loader: `projectos.registry.load_registry`. Validation: `projectos.validation.validate_registry` / `validate_registry_entry`.

Binding rules (fail-closed):

- `repository_root` must be an absolute path and the Git root.
- `project/repository.json` in that repo must declare the same `project_human_id` (`repository_type` = `delivery-project`, `isolation_model` = `one-project-per-repository`).
- `python -m projectctl status` in that repo’s venv must report **exactly one** active project matching the id (`projectctl_bridge.run_projectctl_status`).
- Two registry rows may not share a `project_human_id` or a `repository_root`.
- Jobs from different projects never share a worktree or a projectctl `project.db`.

Registry is **not** SQLite. Identity is file + git + projectctl, persisted onto jobs as `identity_snapshot_json` at create/lease time.

---

## 3. Job lifecycle

Persistence: SQLite table `orchestration_jobs` via `projectos.store`.

**Statuses** (`JOB_STATUSES`):

```
QUEUED → READY → LEASED → RUNNING → SUCCEEDED | FAILED | BLOCKED | RETRY_WAIT | CANCELLED
```

- `QUEUED`: planned but not eligible (typical for RELEASE until integration/QA gates).
- `READY`: eligible if `job_satisfies_dependency` for every predecessor.
- `LEASED` / `RUNNING`: worker owns the row (`worker_leases`).
- `RETRY_WAIT`: retryable failure; `recover` may promote to READY (`promote_retry_wait_to_ready`).
- `BLOCKED`: fail-closed (identity, dirty tree, empty AC, release gate reject, …).
- Terminal: `SUCCEEDED`, `FAILED`, `BLOCKED`, `CANCELLED`.

**Outcomes** (orthogonal to status): `INVALIDATED`, `SUPERSEDED`, `NO_CHANGE`, `SALVAGED`, `GATE_READY`, `GATE_REJECTED`, …  
`SUCCEEDED` + `INVALIDATED` / `SUPERSEDED` / `NO_CHANGE` does **not** satisfy dependents.

**Queues / roles** (`projectos.constants.VALID_QUEUES`, `config/queue-policy.md`):

`PM`, `ARCHITECTURE`, `DELIVERY`, `ASSURANCE_FUNCTIONAL`, `ASSURANCE_INTEGRATION`, `ASSURANCE_SECURITY`, `ASSURANCE_QUALITY`, `INTEGRATION`, `RELEASE`.

Typical Phase 2 graph:

```
PM → ARCHITECTURE → DELIVERY (per story)
                 → ASSURANCE_* + QA_MANAGER (exact candidate SHA)
                 → INTEGRATION (merged candidate)
                 → RELEASE (queued until integration SHA + QA gates)
```

Provenance fields: `base_git_sha`, `candidate_git_sha`, `source_delivery_job_id`, `source_candidate_sha`, `superseded_by_job_id`.

Code-modifying roles (`CODE_MODIFYING_ROLES`: `DELIVERY`, `ARCHITECTURE`, `INTEGRATION`) use isolated git worktrees. RELEASE also uses a worktree **checked out at the integrated SHA** for evaluation; it must not dirty that tree.

---

## 4. Worker, dispatch, recovery

### Worker (`projectos.worker.run_once`)

1. `initialize_database`
2. Short TX: `recover_expired_leases`; select READY job (`select_ready_job`, or `--job` if already `READY`); `acquire_lease`; revalidate registry identity; `mark_running`.
3. RELEASE: `bind_release_provenance` (integration SHA + QA gates).
4. Ensure worktree if required (`ensure_worktree`); RELEASE checks out `source_candidate_sha`.
5. Close SQLite.
6. Execute:
   - Most queues: Cursor headless (`cursor_adapter.invoke_cursor_agent`).
   - RELEASE: **in-process gate** (`release_readiness.evaluate_release_job`); does not invoke Cursor against the product tree. Evidence goes under `logs/runs/<job_human_id>/`.
7. Short TX: persist `agent_runs`; delivery candidate evaluation (`evaluate_delivery_candidate`); RELEASE dirty/SHA/gate checks; `mark_succeeded` or `mark_failure` (`blocked=True` → `BLOCKED`).
8. Side effects: QA handoff after DELIVERY (`qa_handoff`); bind/promote RELEASE after INTEGRATION (`release_provenance`).

Worker **SUCCEEDED is not QA or release approval**. RELEASE success with `outcome=GATE_READY` still is not `python -m projectctl release complete`.

### Dispatch (`projectos.dispatch.run_dispatch`)

- `--once` (default if neither flag) or `--until-idle`.
- Selects `list_eligible_ready_jobs` up to `--max-parallel`.
- Each slot is `run_once(job_human_id=…)`.
- Ctrl+C: bounded drain, `cancel_active_cursor_processes`.

### Recovery (`projectos.recover.run_recovery`)

Default `recover` (no extra flags):

- Expire leases on RUNNING/LEASED (`recover_expired_leases`).
- Promote `RETRY_WAIT` → READY unless `--no-promote`.
- Revalidate identity of active jobs per registry (`RECOVERY_ACTIVE_STATUSES`).
- Reconcile recorded worktrees vs `git worktree list --porcelain`; ignore unknown trees.

Special operators (do not dispatch):

| Flag | Module | Effect |
|------|--------|--------|
| `--salvage-candidate` | `salvage.salvage_delivery_candidate` | FAILED DELIVERY, clean worktree HEAD ≠ base, descendant of base → salvage outcome, new assurance jobs |
| `--reconcile-release` | `release_retry.reconcile_stale_release` | Preserve historical RELEASE; successor bound to SUCCEEDED INTEGRATION SHA |
| `--revalidate-blocked` | `recover.revalidate_blocked_job` | Empty-AC / work-item BLOCKED → READY if assignment now resolvable |
| `--reclaim-running` | `store.reclaim_interrupted_running_job` | Interrupted RUNNING/LEASED → `RETRY_WAIT`, then READY unless `--no-promote` |

---

## 5. Database and logs (ProjectOS)

| Path | Role |
|------|------|
| `state/projectos.db` | Orchestration SQLite (`DEFAULT_DB_PATH`) |
| `migrations/` | Numbered SQL (`001_orchestration.sql`, `002_phase2_consolidation.sql`, `003_delivery_correctness.sql`, …) applied by `projectos.migrate.initialize_database` |
| `config/projects.json` | Registry |
| `logs/runs/<id>/` | Worker/gate evidence (`RUN_OUTPUT_DIR`) |

Principal tables: `orchestration_jobs`, `orchestration_job_dependencies`, `worker_leases`, `agent_runs`, `run_events`, `qa_evidence`, `iteration_runs`, `iteration_run_checkpoints`, `project_schedules`, `daemon_state`, `candidate_invalidations`.

`--db` overrides the orchestration database. Delivery **projectctl** state lives in each delivery repo (`project-control/project.db`), never in ProjectOS SQLite.

Iteration conductor uses `iteration_runs` / `iteration_run_checkpoints` in ProjectOS SQLite. That is **not** the same as projectctl `iterations`.

---

## 6. FAT repositories

Registered in `config/projects.json`:

| ID | `repository_root` | Role in Phase 2 |
|----|-------------------|------------------|
| **PRJ-003** | `C:\Dev\PersonalTaskManager` | Product FAT: ITER-002 due date + priority, integration SHA `5811c17730849fe0282db06690f9d9d7cd5315a1`, REL-002 packaging via **projectctl** in that repo |
| **PRJ-001** | `C:\Dev\Phase2IsolationPilot` | Isolation FAT: second enabled project; must not share worktrees or projectctl DB with PRJ-003 |

`fat reconcile` is implemented **only** for `PRJ-003` / `ITER-002` (`cli.cmd_fat_reconcile` → `reconcile_prj003_iter002_fat`). It invalidates no-op deliveries and creates rework jobs; it does not dispatch.

Delivery governance (releases, iteration status, stories) for PRJ-003 is `python -m projectctl` in PersonalTaskManager. ProjectOS RELEASE gate **reads** that projectctl DB at the **registered** root (`project-control/project.db` via `resolve_authoritative_projectctl_db`), not a git worktree copy.

---

## 7. Seams for an API / application-service layer

Keep HTTP/JSON as a thin adapter. The durable contracts already exist as Python callables + dataclasses.

**Recommended application services** (one façade per operator capability):

| Service | Existing functions | Notes |
|--------|-------------------|--------|
| RegistryService | `load_registry`, `validate_registry` | Read-only identity |
| PlanService | `run_plan` | Already returns `PlanResult` |
| WorkerService | `run_once` | Inject `cursor_runner` / `projectctl_runner` / `release_evaluator` |
| DispatchService | `run_dispatch` | Long-running; pass `cancel_event`; do not block HTTP without a run id |
| RecoverService | `run_recovery`, `salvage_delivery_candidate`, `reconcile_stale_release`, `revalidate_blocked_job`, `reclaim_interrupted_running_job` | Mutually exclusive operators |
| ReleaseReadinessService | `evaluate_release_job`, `assemble_qa_package` | Evidence under `logs/runs` |
| IterationService | `run_iteration` | ProjectOS conductor; not projectctl iteration status |
| OpsService | `run_doctor`, `build_budget_report`, schedule/daemon | Diagnostics |

**Adapters to keep below the service line:**

- `store` — SQLite only
- `registry` + `validation` + `repository` + `gitutil` — identity
- `projectctl_bridge` — subprocess to delivery `python -m projectctl` (ProjectOS should not grow a second copy of release-lifecycle SQL)
- `worktree` / `cursor_adapter` — git and Cursor
- `cli` — argparse + stdout only

**Do not** put orchestration policy in HTTP handlers. Do not have handlers `INSERT` into `orchestration_jobs`.

---

## 8. CLI logic to extract (do not duplicate in HTTP)

Today `cli.py` is a large argparse file with **presentation** mixed into command functions. Extract before adding an API:

1. **Result → DTO / formatter**  
   `cmd_recover` (salvage / reconcile-release / revalidate-blocked / reclaim-running / default `RecoveryReport`) repeats field printing (`job:`, `status:`, `candidate_git_sha:`, …). Same for `cmd_worker`, `cmd_fat_reconcile`, `cmd_registry_*`, `cmd_cursor_smoke`. An API should return the dataclasses already defined in the modules (`WorkerResult`, `ReleaseReconcileResult`, `RecoveryReport`, `SalvageResult`, …). One formatter for CLI; JSON for HTTP.

2. **`--job` required-flag checks**  
   Four recover operators each `if not args.job: print error; return 1`. That belongs in the service (`OrchestrationError`) so CLI and API share the same validation.

3. **`--db` / `--config` defaults**  
   Every command repeats `_add_db_arg`. A request context `{db_path, registry_path}` should be constructed once (CLI flags or API config) and passed into services.

4. **Exit-code mapping**  
   `main()` maps `ProjectOSError` → 1 and other exceptions → 1. HTTP should map the same types (4xx for identity/not-found, 409 for lifecycle conflict, 500 for unexpected). Do not invent a second error taxonomy in the router.

5. **FAT-only branch**  
   `cmd_fat_reconcile` hard-codes `PRJ-003` / `ITER-002`. Keep that in `invalidate.reconcile_prj003_iter002_fat`; CLI/API only pass project/iteration.

6. **Dispatch / daemon long poll**  
   HTTP should not block on `--until-idle` without a job/run id. Reuse `run_dispatch` with `cancel_event`; do not copy the `ThreadPoolExecutor` loop.

7. **projectctl mutations**  
   REL-002 complete and iteration reconcile live in **PersonalTaskManager projectctl**, not in ProjectOS CLI. An API that “releases” PRJ-003 must call `projectctl_bridge.run_projectctl` (registered root + `--db project-control/project.db`), not duplicate `release_lifecycle.py`.

---

## 9. What Phase 3 should not relitigate

- One project per repository (`isolation_model`).
- Exact SHA QA handoff (`source_candidate_sha`).
- Cursor exit 0 ≠ delivery success (`evaluate_delivery_candidate`).
- RELEASE worker success ≠ `projectctl release complete`.
- Historical FAILED/SUCCEEDED rows are not rewritten to fake a happy path; salvage/reconcile create successors and `run_events`.
- Isolation: PRJ-001 and PRJ-003 stay separate git roots and projectctl databases.
