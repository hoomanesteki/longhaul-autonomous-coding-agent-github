import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import MemoryPage from "./pages/MemoryPage";
import ReposPage from "./pages/ReposPage";
import TasksPage from "./pages/TasksPage";

/** The routes of the console. The router itself is set up by main.tsx, and by the tests. */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/tasks" replace />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="repos" element={<ReposPage />} />
        <Route path="*" element={<Navigate to="/tasks" replace />} />
      </Route>
    </Routes>
  );
}
