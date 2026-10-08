import { Navigate, type RouteObject } from "react-router";

import { AppLayout } from "./components/AppLayout";
import { DocumentDetailPage } from "./documents/DocumentDetailPage";
import { DocumentsPage } from "./documents/DocumentsPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";

export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  {
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <Navigate to="/documents" replace /> },
          { path: "/documents", element: <DocumentsPage /> },
          { path: "/documents/:documentId", element: <DocumentDetailPage /> },
          { path: "/status", element: <SystemStatusPage /> },
        ],
      },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
];
