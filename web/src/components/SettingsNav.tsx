import { NavLink } from "react-router-dom";

export function SettingsNav() {
  return (
    <nav className="project-nav" aria-label="Settings sections">
      <NavLink to="/settings" end>
        General
      </NavLink>
      <NavLink to="/settings/integrations/slack">Slack</NavLink>
      <NavLink to="/settings/integrations/openai">OpenAI</NavLink>
    </nav>
  );
}
