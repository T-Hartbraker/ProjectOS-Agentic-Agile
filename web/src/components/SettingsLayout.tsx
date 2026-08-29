import { Outlet } from "react-router-dom";
import { SettingsNav } from "./SettingsNav";

export function SettingsLayout() {
  return (
    <section className="page">
      <h1>Settings</h1>
      <p className="muted">Global ProjectOS configuration. These settings apply across all projects.</p>
      <SettingsNav />
      <Outlet />
    </section>
  );
}
