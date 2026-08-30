-- Useful indexes for Phase 1 queries

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_is_active ON projects(is_active);

CREATE INDEX IF NOT EXISTS idx_requirements_project ON requirements(project_id);
CREATE INDEX IF NOT EXISTS idx_requirements_status ON requirements(status);

CREATE INDEX IF NOT EXISTS idx_acceptance_criteria_requirement ON acceptance_criteria(requirement_id);

CREATE INDEX IF NOT EXISTS idx_epics_project ON epics(project_id);
CREATE INDEX IF NOT EXISTS idx_features_project ON features(project_id);
CREATE INDEX IF NOT EXISTS idx_features_epic ON features(epic_id);

CREATE INDEX IF NOT EXISTS idx_stories_project ON stories(project_id);
CREATE INDEX IF NOT EXISTS idx_stories_feature ON stories(feature_id);
CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_story ON tasks(story_id);

CREATE INDEX IF NOT EXISTS idx_iterations_project ON iterations(project_id);
CREATE INDEX IF NOT EXISTS idx_iteration_items_iteration ON iteration_items(iteration_id);

CREATE INDEX IF NOT EXISTS idx_releases_project ON releases(project_id);
CREATE INDEX IF NOT EXISTS idx_defects_project ON defects(project_id);
CREATE INDEX IF NOT EXISTS idx_defects_status ON defects(status);

CREATE INDEX IF NOT EXISTS idx_test_cases_project ON test_cases(project_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_test_case ON test_runs(test_case_id);

CREATE INDEX IF NOT EXISTS idx_risks_project ON risks(project_id);
CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id);
CREATE INDEX IF NOT EXISTS idx_assumptions_project ON assumptions(project_id);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id);
CREATE INDEX IF NOT EXISTS idx_change_requests_project ON change_requests(project_id);

CREATE INDEX IF NOT EXISTS idx_agent_assignments_agent ON agent_assignments(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id);

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id);

CREATE INDEX IF NOT EXISTS idx_trace_links_project ON trace_links(project_id);
CREATE INDEX IF NOT EXISTS idx_trace_links_source ON trace_links(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_trace_links_target ON trace_links(target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_token_ledger_project ON token_ledger(project_id);
CREATE INDEX IF NOT EXISTS idx_token_ledger_run ON token_ledger(agent_run_id);

CREATE INDEX IF NOT EXISTS idx_improvements_project ON improvements(project_id);

CREATE INDEX IF NOT EXISTS idx_custom_field_definitions_project ON custom_field_definitions(project_id);
CREATE INDEX IF NOT EXISTS idx_custom_field_values_definition ON custom_field_values(definition_id);
CREATE INDEX IF NOT EXISTS idx_custom_field_values_entity ON custom_field_values(entity_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
