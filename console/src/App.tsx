import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import RequireToken from "./components/RequireToken";
import Login from "./pages/Login";
import MemoryPage from "./pages/MemoryPage";
import ReposPage from "./pages/ReposPage";
import TaskPage from "./pages/TaskPage";
import TasksPage from "./pages/TasksPage";

/**
 * The routes of the console. The router itself is set up by main.tsx, and by the tests. Every page
 * is behind the token gate; the login is the one route outside it.
 */
export default function App() {
  return (
    <Routes>
      <Route path="login" element={<Login />} />
      <Route element={<RequireToken />}>
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/tasks" replace />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="tasks/:taskId" element={<TaskPage />} />
          <Route path="memory" element={<MemoryPage />} />
          <Route path="repos" element={<ReposPage />} />
          <Route path="*" element={<Navigate to="/tasks" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
