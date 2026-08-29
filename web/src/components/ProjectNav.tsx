import { NavLink } from "react-router-dom";

type Props = {
  projectHumanId: string;
};

export function ProjectNav({ projectHumanId }: Props) {
  const base = `/projects/${encodeURIComponent(projectHumanId)}`;
  return (
    <nav className="project-nav" aria-label="Project sections">
      <NavLink to={base} end>
        Overview
      </NavLink>
      <NavLink to={`${base}/intake`}>New Work</NavLink>
      <NavLink to={`${base}/jobs`}>Work</NavLink>
      <NavLink to={`${base}/quality`}>Quality</NavLink>
      <NavLink to={`${base}/releases`}>Releases</NavLink>
      <NavLink to={`${base}/reports`}>Reports</NavLink>
      <NavLink to={`${base}/learning`}>Learning</NavLink>
      <NavLink to={`${base}/decisions`}>Decisions</NavLink>
      <NavLink to={`${base}/audit`}>Activity</NavLink>
      <NavLink to={`${base}/slack`}>Slack</NavLink>
    </nav>
  );
}
