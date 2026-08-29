import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { HomePage } from "./pages/HomePage";
import { IntakePage } from "./pages/IntakePage";
import { JobsPage } from "./pages/JobsPage";
import { ProjectPage } from "./pages/ProjectPage";
import { QualityPage } from "./pages/QualityPage";
import { ReleaseCenterPage } from "./pages/ReleaseCenterPage";
import { ReportsPage } from "./pages/ReportsPage";
import { LearningPage } from "./pages/LearningPage";
import { DecisionsPage } from "./pages/DecisionsPage";
import { SlackPage } from "./pages/SlackPage";
import { AuditPage } from "./pages/AuditPage";
import { NewProjectPage } from "./pages/NewProjectPage";
import { SettingsLayout } from "./components/SettingsLayout";
import { GeneralSettingsPage } from "./pages/GeneralSettingsPage";
import { SlackSettingsPage } from "./pages/SlackSettingsPage";
import { OpenAISettingsPage } from "./pages/OpenAISettingsPage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/projects/new" element={<NewProjectPage />} />
        <Route path="/projects/:projectHumanId" element={<ProjectPage />} />
        <Route path="/projects/:projectHumanId/intake" element={<IntakePage />} />
        <Route path="/projects/:projectHumanId/jobs" element={<JobsPage />} />
        <Route path="/projects/:projectHumanId/quality" element={<QualityPage />} />
        <Route path="/projects/:projectHumanId/releases" element={<ReleaseCenterPage />} />
        <Route
          path="/projects/:projectHumanId/releases/:releaseHumanId"
          element={<ReleaseCenterPage />}
        />
        <Route path="/projects/:projectHumanId/learning" element={<LearningPage />} />
        <Route path="/projects/:projectHumanId/decisions" element={<DecisionsPage />} />
        <Route path="/projects/:projectHumanId/slack" element={<SlackPage />} />
        <Route path="/projects/:projectHumanId/audit" element={<AuditPage />} />
        <Route path="/projects/:projectHumanId/reports" element={<ReportsPage />} />
        <Route
          path="/projects/:projectHumanId/reports/snapshots/:snapshotHumanId"
          element={<ReportsPage />}
        />
        <Route path="/projects/:projectHumanId/reports/:kind" element={<ReportsPage />} />
        <Route path="/settings" element={<SettingsLayout />}>
          <Route index element={<GeneralSettingsPage />} />
          <Route path="integrations/slack" element={<SlackSettingsPage />} />
          <Route path="integrations/openai" element={<OpenAISettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
