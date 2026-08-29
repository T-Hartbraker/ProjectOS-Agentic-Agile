import type { ProjectResponse } from "../api/types";

type Props = {
  projects: ProjectResponse[];
  selectedId: string | null;
  onSelect: (projectHumanId: string | null) => void;
  onNewProject?: () => void;
  disabled?: boolean;
};

export function ProjectSelector({ projects, selectedId, onSelect, onNewProject, disabled }: Props) {
  return (
    <div className="selector">
      <label htmlFor="project-selector">Project</label>
      <select
        id="project-selector"
        value={selectedId ?? ""}
        disabled={disabled}
        onChange={(event) => {
          const value = event.target.value;
          if (value === "__new__") {
            onNewProject?.();
            return;
          }
          onSelect(value === "" ? null : value);
        }}
      >
        <option value="">Select a project</option>
        <option value="__new__">+ New project</option>
        {projects.map((project) => (
          <option key={project.project_human_id} value={project.project_human_id}>
            {project.project_human_id}
            {project.enabled ? "" : " (disabled)"}
          </option>
        ))}
      </select>
    </div>
  );
}
