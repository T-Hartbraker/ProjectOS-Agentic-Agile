"""argparse CLI for projectctl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from projectctl import store
from projectctl.isolation import ProjectIsolationError, validate_project_isolation
from projectctl.paths import DEFAULT_DB_PATH
from projectctl.repository import RepositoryIdentityError
from projectctl.store import StoreError

# Commands that require one active project matching repository.json.
PROJECT_SCOPED_COMMANDS = frozenset(
    {
        "status",
        "requirement",
        "story",
        "defect",
        "risk",
        "assumption",
        "decision",
        "iteration",
        "release",
        "trace",
        "customfield",
    }
)


def _add_db_arg(
    parser: argparse.ArgumentParser,
    *,
    with_default: bool = False,
) -> None:
    """Add --db. Subparsers must use SUPPRESS so they do not overwrite parent --db."""
    kwargs: dict = {
        "type": Path,
        "help": f"SQLite database path (default: {DEFAULT_DB_PATH})",
    }
    if with_default:
        kwargs["default"] = None
    else:
        kwargs["default"] = argparse.SUPPRESS
    parser.add_argument("--db", **kwargs)


def _db(args: argparse.Namespace) -> Path | None:
    return getattr(args, "db", None)


def _repo_root(args: argparse.Namespace) -> Path | None:
    root = getattr(args, "repo_root", None)
    return Path(root) if root else None


def _enforce_isolation(args: argparse.Namespace) -> None:
    """Fail closed for project-scoped commands."""
    validate_project_isolation(db_path=_db(args), repo_root=_repo_root(args))


def _print_row(row: dict[str, Any]) -> None:
    for key, value in row.items():
        print(f"{key}: {value}")


def _print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(row.get(c, ""))))
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def cmd_init(args: argparse.Namespace) -> int:
    applied = store.init_database(db_path=_db(args))
    path = _db(args) or DEFAULT_DB_PATH
    print(f"Initialized database: {path}")
    if applied:
        print("Applied migrations:")
        for version in applied:
            print(f"  - {version}")
    else:
        print("No new migrations applied (already up to date).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    status = store.get_status(db_path=_db(args))
    print(status["message"])
    if status.get("active_project"):
        ap = status["active_project"]
        print(f"Status: {ap.get('status')}")
        print(f"Created: {ap.get('created_at')}")
        counts = status.get("counts") or {}
        if counts:
            print("Counts:")
            for key, value in counts.items():
                print(f"  {key}: {value}")
    return 0


def cmd_audit_show(args: argparse.Namespace) -> int:
    rows = store.list_audit(db_path=_db(args), limit=args.limit)
    if not rows:
        print("(no audit records)")
        return 0
    for row in rows:
        print(
            f"[{row['id']}] {row['timestamp']} "
            f"{row['action']} {row['entity_type']} {row.get('entity_id') or ''}"
        )
        if args.verbose:
            print(f"  actor: {row.get('actor_type')}/{row.get('actor_id')}")
            print(f"  reason: {row.get('reason')}")
            if row.get("after_state"):
                print(f"  after: {row['after_state']}")
    return 0


def cmd_project_init_repository(args: argparse.Namespace) -> int:
    from projectctl.template_ops import init_repository

    result = init_repository(
        args.name,
        description=args.description,
        repo_root=_repo_root(args),
        db_path=_db(args),
    )
    print(
        f"Initialized repository as delivery-project "
        f"{result.project['human_id']}: {result.project['name']}"
    )
    print(f"Updated identity: {result.repository_path}")
    return 0


def cmd_template_prepare(args: argparse.Namespace) -> int:
    from projectctl.template_ops import prepare_template

    result = prepare_template(
        force=bool(args.force),
        repo_root=_repo_root(args),
        db_path=_db(args),
    )
    print(f"Prepared delivery-template at {result.repo_root}")
    print(f"Database: {result.database_path}")
    print(f"Identity: {result.repository_path}")
    for action in result.actions:
        print(f"  - {action}")
    if result.reported_project_specific_paths:
        print("Project-specific paths left in place (not deleted):")
        for p in result.reported_project_specific_paths:
            print(f"  - {p}")
    return 0


def cmd_project_create(args: argparse.Namespace) -> int:
    row = store.create_project(
        args.name,
        description=args.description,
        db_path=_db(args),
        make_active=not args.inactive,
        enforce_isolation=True,
        repo_root=_repo_root(args),
    )
    print(f"Created project {row['human_id']}: {row['name']}")
    if not row.get("is_active"):
        print("(inactive — historical/smoke record; not the delivery project)")
    return 0


def cmd_project_activate(args: argparse.Namespace) -> int:
    row = store.activate_project(
        args.human_id,
        db_path=_db(args),
        enforce_isolation=True,
        repo_root=_repo_root(args),
        reason=args.reason,
    )
    print(f"Activated project {row['human_id']}: {row['name']}")
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    rows = store.list_projects(db_path=_db(args))
    _print_table(rows, ["human_id", "name", "status", "is_active"])
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    row = store.show_project(args.human_id, db_path=_db(args))
    _print_row(row)
    return 0


def _make_create_list_show(
    *,
    create_fn: Callable[..., dict[str, Any]] | None,
    list_fn: Callable[..., list[dict[str, Any]]],
    show_fn: Callable[..., dict[str, Any]],
    list_columns: list[str],
    create_kwargs_from_args: Callable[[argparse.Namespace], dict[str, Any]] | None = None,
) -> tuple[Callable, Callable, Callable]:
    def do_create(args: argparse.Namespace) -> int:
        assert create_fn is not None
        kwargs = create_kwargs_from_args(args) if create_kwargs_from_args else {}
        kwargs["db_path"] = _db(args)
        if hasattr(args, "project") and args.project:
            kwargs["project_id"] = args.project
        row = create_fn(**kwargs)
        print(f"Created {row.get('human_id')}")
        return 0

    def do_list(args: argparse.Namespace) -> int:
        kwargs: dict[str, Any] = {"db_path": _db(args)}
        if hasattr(args, "project") and args.project:
            kwargs["project_id"] = args.project
        rows = list_fn(**kwargs)
        _print_table(rows, list_columns)
        return 0

    def do_show(args: argparse.Namespace) -> int:
        row = show_fn(args.human_id, db_path=_db(args))
        _print_row(row)
        return 0

    return do_create, do_list, do_show


def cmd_release_status(args: argparse.Namespace) -> int:
    target = args.target_status.strip().lower()
    if target == "released":
        print(
            "error: use 'release complete' to enter released "
            "(cannot set released via status bypass)",
            file=sys.stderr,
        )
        return 1
    row = store.transition_release_status(
        args.human_id,
        target,
        git_sha=args.git_sha,
        dirty_tree_exception=args.dirty_tree_exception,
        artifact_ref=args.artifact_ref,
        qa_evidence_ref=args.qa_evidence_ref,
        iteration_id=args.iteration,
        reason=args.reason,
        db_path=_db(args),
    )
    print(
        f"Release {row['human_id']} status -> {row['status']}"
        + (f" git_sha={row.get('git_sha')}" if row.get("git_sha") else "")
    )
    return 0


def cmd_release_complete(args: argparse.Namespace) -> int:
    row = store.complete_release(
        args.human_id,
        artifact_ref=args.artifact_ref,
        reason=args.reason,
        db_path=_db(args),
    )
    print(
        f"Release {row['human_id']} released "
        f"at {row.get('released_at')} git_sha={row.get('git_sha')}"
    )
    return 0


def cmd_trace_create(args: argparse.Namespace) -> int:
    row = store.create_trace_link(
        source_type=args.source_type,
        source_id=args.source_id,
        link_type=args.link_type,
        target_type=args.target_type,
        target_id=args.target_id,
        project_id=args.project,
        db_path=_db(args),
    )
    print(
        f"Created trace link {row['id']}: "
        f"{row['source_id']} -> {row['link_type']} -> {row['target_id']}"
    )
    return 0


def cmd_trace_list(args: argparse.Namespace) -> int:
    rows = store.list_trace_links(project_id=args.project, db_path=_db(args))
    _print_table(
        rows,
        ["id", "source_type", "source_id", "link_type", "target_type", "target_id"],
    )
    return 0


def cmd_customfield_define(args: argparse.Namespace) -> int:
    row = store.create_custom_field_definition(
        entity_type=args.entity_type,
        field_key=args.field_key,
        display_name=args.display_name,
        data_type=args.data_type,
        description=args.description,
        project_id=args.project,
        db_path=_db(args),
    )
    print(f"Created custom field definition id={row['id']} ({row['field_key']})")
    return 0


def cmd_customfield_set(args: argparse.Namespace) -> int:
    value: Any = args.value
    if args.json_value:
        value = json.loads(args.value)
    row = store.set_custom_field_value(
        definition_id=args.definition_id,
        entity_id=args.entity_id,
        value=value,
        db_path=_db(args),
    )
    print(f"Set custom field value id={row['id']} for {args.entity_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="projectctl",
        description="Phase 1 project-control CLI",
    )
    _add_db_arg(parser, with_default=True)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root containing project/repository.json "
        "(default: discover by walking from cwd)",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Initialize database and apply migrations")
    _add_db_arg(p_init)
    p_init.set_defaults(func=cmd_init)

    # status
    p_status = sub.add_parser("status", help="Show project status summary")
    _add_db_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    # audit
    p_audit = sub.add_parser("audit", help="Audit log commands")
    audit_sub = p_audit.add_subparsers(dest="audit_command", required=True)
    p_audit_show = audit_sub.add_parser("show", help="Show recent audit records")
    _add_db_arg(p_audit_show)
    p_audit_show.add_argument("--limit", type=int, default=50)
    p_audit_show.add_argument("-v", "--verbose", action="store_true")
    p_audit_show.set_defaults(func=cmd_audit_show)

    # project
    p_project = sub.add_parser("project", help="Project commands")
    project_sub = p_project.add_subparsers(dest="project_command", required=True)

    p_pc = project_sub.add_parser("create", help="Create a project")
    _add_db_arg(p_pc)
    p_pc.add_argument("--name", required=True)
    p_pc.add_argument("--description", default=None)
    p_pc.add_argument(
        "--inactive",
        action="store_true",
        help="Create as inactive (historical/smoke); required when an active "
        "delivery project already exists under repository isolation",
    )
    p_pc.set_defaults(func=cmd_project_create)

    p_pi = project_sub.add_parser(
        "init-repository",
        help="Bind an unbound delivery-template to a new active delivery project",
    )
    _add_db_arg(p_pi)
    p_pi.add_argument("--name", required=True, help="New delivery project name")
    p_pi.add_argument("--description", default=None)
    p_pi.set_defaults(func=cmd_project_init_repository)

    p_pa = project_sub.add_parser(
        "activate",
        help="Activate a project (must match repository.json project_human_id)",
    )
    _add_db_arg(p_pa)
    p_pa.add_argument("human_id")
    p_pa.add_argument("--reason", default=None)
    p_pa.set_defaults(func=cmd_project_activate)

    p_pl = project_sub.add_parser("list", help="List projects")
    _add_db_arg(p_pl)
    p_pl.set_defaults(func=cmd_project_list)

    p_ps = project_sub.add_parser("show", help="Show a project")
    _add_db_arg(p_ps)
    p_ps.add_argument("human_id")
    p_ps.set_defaults(func=cmd_project_show)

    # template
    p_tmpl = sub.add_parser("template", help="Delivery-template commands")
    tmpl_sub = p_tmpl.add_subparsers(dest="template_command", required=True)
    p_tp = tmpl_sub.add_parser(
        "prepare",
        help="Convert this repo into an unbound delivery-template (requires --force)",
    )
    _add_db_arg(p_tp)
    p_tp.add_argument(
        "--force",
        action="store_true",
        help="Required. Reset local project-control DB and unbound repository.json",
    )
    p_tp.set_defaults(func=cmd_template_prepare)

    def _add_project_option(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--project",
            default=None,
            help="Project human id (default: active project)",
        )

    # requirement
    req_create, req_list, req_show = _make_create_list_show(
        create_fn=store.create_requirement,
        list_fn=store.list_requirements,
        show_fn=store.show_requirement,
        list_columns=["human_id", "title", "status", "priority"],
        create_kwargs_from_args=lambda a: {
            "title": a.title,
            "description": a.description,
        },
    )
    p_req = sub.add_parser("requirement", help="Requirement commands")
    req_sub = p_req.add_subparsers(dest="requirement_command", required=True)
    p_rc = req_sub.add_parser("create", help="Create a requirement")
    _add_db_arg(p_rc)
    _add_project_option(p_rc)
    p_rc.add_argument("--title", required=True)
    p_rc.add_argument("--description", default=None)
    p_rc.set_defaults(func=req_create)
    p_rl = req_sub.add_parser("list", help="List requirements")
    _add_db_arg(p_rl)
    _add_project_option(p_rl)
    p_rl.set_defaults(func=req_list)
    p_rs = req_sub.add_parser("show", help="Show a requirement")
    _add_db_arg(p_rs)
    p_rs.add_argument("human_id")
    p_rs.set_defaults(func=req_show)

    # story
    story_create, story_list, story_show = _make_create_list_show(
        create_fn=store.create_story,
        list_fn=store.list_stories,
        show_fn=store.show_story,
        list_columns=["human_id", "title", "status", "priority"],
        create_kwargs_from_args=lambda a: {
            "title": a.title,
            "description": a.description,
        },
    )
    p_story = sub.add_parser("story", help="Story commands")
    story_sub = p_story.add_subparsers(dest="story_command", required=True)
    p_sc = story_sub.add_parser("create", help="Create a story")
    _add_db_arg(p_sc)
    _add_project_option(p_sc)
    p_sc.add_argument("--title", required=True)
    p_sc.add_argument("--description", default=None)
    p_sc.set_defaults(func=story_create)
    p_sl = story_sub.add_parser("list", help="List stories")
    _add_db_arg(p_sl)
    _add_project_option(p_sl)
    p_sl.set_defaults(func=story_list)
    p_ss = story_sub.add_parser("show", help="Show a story")
    _add_db_arg(p_ss)
    p_ss.add_argument("human_id")
    p_ss.set_defaults(func=story_show)

    # defect
    defect_create, defect_list, defect_show = _make_create_list_show(
        create_fn=store.create_defect,
        list_fn=store.list_defects,
        show_fn=store.show_defect,
        list_columns=["human_id", "title", "severity", "status"],
        create_kwargs_from_args=lambda a: {
            "title": a.title,
            "description": a.description,
            "severity": a.severity,
        },
    )
    p_def = sub.add_parser("defect", help="Defect commands")
    def_sub = p_def.add_subparsers(dest="defect_command", required=True)
    p_dc = def_sub.add_parser("create", help="Create a defect")
    _add_db_arg(p_dc)
    _add_project_option(p_dc)
    p_dc.add_argument("--title", required=True)
    p_dc.add_argument("--description", default=None)
    p_dc.add_argument("--severity", default="medium")
    p_dc.set_defaults(func=defect_create)
    p_dl = def_sub.add_parser("list", help="List defects")
    _add_db_arg(p_dl)
    _add_project_option(p_dl)
    p_dl.set_defaults(func=defect_list)
    p_ds = def_sub.add_parser("show", help="Show a defect")
    _add_db_arg(p_ds)
    p_ds.add_argument("human_id")
    p_ds.set_defaults(func=defect_show)

    # risk
    risk_create, risk_list, risk_show = _make_create_list_show(
        create_fn=store.create_risk,
        list_fn=store.list_risks,
        show_fn=store.show_risk,
        list_columns=["human_id", "title", "status"],
        create_kwargs_from_args=lambda a: {
            "title": a.title,
            "description": a.description,
        },
    )
    p_risk = sub.add_parser("risk", help="Risk commands")
    risk_sub = p_risk.add_subparsers(dest="risk_command", required=True)
    p_rkc = risk_sub.add_parser("create", help="Create a risk")
    _add_db_arg(p_rkc)
    _add_project_option(p_rkc)
    p_rkc.add_argument("--title", required=True)
    p_rkc.add_argument("--description", default=None)
    p_rkc.set_defaults(func=risk_create)
    p_rkl = risk_sub.add_parser("list", help="List risks")
    _add_db_arg(p_rkl)
    _add_project_option(p_rkl)
    p_rkl.set_defaults(func=risk_list)
    p_rks = risk_sub.add_parser("show", help="Show a risk")
    _add_db_arg(p_rks)
    p_rks.add_argument("human_id")
    p_rks.set_defaults(func=risk_show)

    # assumption
    asm_create, asm_list, asm_show = _make_create_list_show(
        create_fn=store.create_assumption,
        list_fn=store.list_assumptions,
        show_fn=store.show_assumption,
        list_columns=["human_id", "statement", "status"],
        create_kwargs_from_args=lambda a: {"statement": a.statement},
    )
    p_asm = sub.add_parser("assumption", help="Assumption commands")
    asm_sub = p_asm.add_subparsers(dest="assumption_command", required=True)
    p_ac = asm_sub.add_parser("create", help="Create an assumption")
    _add_db_arg(p_ac)
    _add_project_option(p_ac)
    p_ac.add_argument("--statement", required=True)
    p_ac.set_defaults(func=asm_create)
    p_al = asm_sub.add_parser("list", help="List assumptions")
    _add_db_arg(p_al)
    _add_project_option(p_al)
    p_al.set_defaults(func=asm_list)
    p_as = asm_sub.add_parser("show", help="Show an assumption")
    _add_db_arg(p_as)
    p_as.add_argument("human_id")
    p_as.set_defaults(func=asm_show)

    # decision
    dec_create, dec_list, dec_show = _make_create_list_show(
        create_fn=store.create_decision,
        list_fn=store.list_decisions,
        show_fn=store.show_decision,
        list_columns=["human_id", "title", "status"],
        create_kwargs_from_args=lambda a: {
            "title": a.title,
            "decision": a.decision,
            "rationale": a.rationale,
        },
    )
    p_dec = sub.add_parser("decision", help="Decision commands")
    dec_sub = p_dec.add_subparsers(dest="decision_command", required=True)
    p_dec_c = dec_sub.add_parser("create", help="Create a decision")
    _add_db_arg(p_dec_c)
    _add_project_option(p_dec_c)
    p_dec_c.add_argument("--title", required=True)
    p_dec_c.add_argument("--decision", required=True)
    p_dec_c.add_argument("--rationale", default=None)
    p_dec_c.set_defaults(func=dec_create)
    p_dec_l = dec_sub.add_parser("list", help="List decisions")
    _add_db_arg(p_dec_l)
    _add_project_option(p_dec_l)
    p_dec_l.set_defaults(func=dec_list)
    p_dec_s = dec_sub.add_parser("show", help="Show a decision")
    _add_db_arg(p_dec_s)
    p_dec_s.add_argument("human_id")
    p_dec_s.set_defaults(func=dec_show)

    # iteration
    iter_create, iter_list, iter_show = _make_create_list_show(
        create_fn=store.create_iteration,
        list_fn=store.list_iterations,
        show_fn=store.show_iteration,
        list_columns=["human_id", "name", "status"],
        create_kwargs_from_args=lambda a: {"name": a.name, "goal": a.goal},
    )
    p_iter = sub.add_parser("iteration", help="Iteration commands")
    iter_sub = p_iter.add_subparsers(dest="iteration_command", required=True)
    p_ic = iter_sub.add_parser("create", help="Create an iteration")
    _add_db_arg(p_ic)
    _add_project_option(p_ic)
    p_ic.add_argument("--name", required=True)
    p_ic.add_argument("--goal", default=None)
    p_ic.set_defaults(func=iter_create)
    p_il = iter_sub.add_parser("list", help="List iterations")
    _add_db_arg(p_il)
    _add_project_option(p_il)
    p_il.set_defaults(func=iter_list)
    p_is = iter_sub.add_parser("show", help="Show an iteration")
    _add_db_arg(p_is)
    p_is.add_argument("human_id")
    p_is.set_defaults(func=iter_show)

    # release
    rel_create, rel_list, rel_show = _make_create_list_show(
        create_fn=store.create_release,
        list_fn=store.list_releases,
        show_fn=store.show_release,
        list_columns=["human_id", "name", "version", "status", "git_sha", "iteration"],
        create_kwargs_from_args=lambda a: {
            "name": a.name,
            "version": a.version,
            "iteration_id": a.iteration,
        },
    )
    p_rel = sub.add_parser("release", help="Release commands")
    rel_sub = p_rel.add_subparsers(dest="release_command", required=True)
    p_rel_c = rel_sub.add_parser("create", help="Create a release")
    _add_db_arg(p_rel_c)
    _add_project_option(p_rel_c)
    p_rel_c.add_argument("--name", required=True)
    p_rel_c.add_argument("--version", default=None)
    p_rel_c.add_argument(
        "--iteration",
        default=None,
        help="Iteration human id (e.g. ITER-001)",
    )
    p_rel_c.set_defaults(func=rel_create)
    p_rel_l = rel_sub.add_parser("list", help="List releases")
    _add_db_arg(p_rel_l)
    _add_project_option(p_rel_l)
    p_rel_l.set_defaults(func=rel_list)
    p_rel_s = rel_sub.add_parser("show", help="Show a release")
    _add_db_arg(p_rel_s)
    p_rel_s.add_argument("human_id")
    p_rel_s.set_defaults(func=rel_show)

    p_rel_st = rel_sub.add_parser(
        "status",
        help="Transition release status (planned|candidate|qa_passed|...)",
    )
    _add_db_arg(p_rel_st)
    p_rel_st.add_argument("human_id", help="Release id (e.g. REL-001)")
    p_rel_st.add_argument(
        "target_status",
        help="Target status: planned|candidate|qa_passed|released|superseded|withdrawn",
    )
    p_rel_st.add_argument(
        "--git-sha",
        default=None,
        help="Exact git commit SHA (required for candidate)",
    )
    p_rel_st.add_argument(
        "--dirty-tree-exception",
        default=None,
        metavar="DEC-ID",
        help="Approved decision id allowing dirty/historical candidate",
    )
    p_rel_st.add_argument(
        "--artifact-ref",
        default=None,
        help="Path to release package dir or notes artifact",
    )
    p_rel_st.add_argument(
        "--qa-evidence",
        default=None,
        dest="qa_evidence_ref",
        help="Path to independent QA recommendation (required for qa_passed)",
    )
    p_rel_st.add_argument(
        "--iteration",
        default=None,
        help="Iteration human id to associate",
    )
    p_rel_st.add_argument("--reason", default=None)
    p_rel_st.set_defaults(func=cmd_release_status)

    p_rel_done = rel_sub.add_parser(
        "complete",
        help="Promote qa_passed -> released (rejects prerequisite bypass)",
    )
    _add_db_arg(p_rel_done)
    p_rel_done.add_argument("human_id")
    p_rel_done.add_argument(
        "--artifact-ref",
        default=None,
        help="Release package path if not already recorded",
    )
    p_rel_done.add_argument("--reason", default=None)
    p_rel_done.set_defaults(func=cmd_release_complete)

    # trace
    p_trace = sub.add_parser("trace", help="Traceability commands")
    trace_sub = p_trace.add_subparsers(dest="trace_command", required=True)
    p_tc = trace_sub.add_parser("create", help="Create a trace link")
    _add_db_arg(p_tc)
    _add_project_option(p_tc)
    p_tc.add_argument("--source-type", required=True)
    p_tc.add_argument("--source-id", required=True)
    p_tc.add_argument("--link-type", required=True)
    p_tc.add_argument("--target-type", required=True)
    p_tc.add_argument("--target-id", required=True)
    p_tc.set_defaults(func=cmd_trace_create)
    p_tl = trace_sub.add_parser("list", help="List trace links")
    _add_db_arg(p_tl)
    _add_project_option(p_tl)
    p_tl.set_defaults(func=cmd_trace_list)

    # customfield (supports Phase 1 custom fields without ALTER TABLE)
    p_cf = sub.add_parser("customfield", help="Custom field commands")
    cf_sub = p_cf.add_subparsers(dest="customfield_command", required=True)
    p_cfd = cf_sub.add_parser("define", help="Define a custom field")
    _add_db_arg(p_cfd)
    _add_project_option(p_cfd)
    p_cfd.add_argument("--entity-type", required=True)
    p_cfd.add_argument("--field-key", required=True)
    p_cfd.add_argument("--display-name", required=True)
    p_cfd.add_argument(
        "--data-type",
        required=True,
        choices=sorted(store.ALLOWED_CUSTOM_TYPES),
    )
    p_cfd.add_argument("--description", default=None)
    p_cfd.set_defaults(func=cmd_customfield_define)
    p_cfs = cf_sub.add_parser("set", help="Set a custom field value")
    _add_db_arg(p_cfs)
    p_cfs.add_argument("--definition-id", type=int, required=True)
    p_cfs.add_argument("--entity-id", required=True)
    p_cfs.add_argument("--value", required=True)
    p_cfs.add_argument(
        "--json-value",
        action="store_true",
        help="Parse --value as JSON",
    )
    p_cfs.set_defaults(func=cmd_customfield_set)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 1

    if not getattr(args, "command", None) or not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        command = args.command
        if command in PROJECT_SCOPED_COMMANDS:
            _enforce_isolation(args)
        return int(args.func(args))
    except (StoreError, ProjectIsolationError, RepositoryIdentityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface unexpected CLI failures cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
