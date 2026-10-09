import { Navigate, type RouteObject } from "react-router";

import { ComparisonPage } from "./comparisons/ComparisonPage";
import { AppLayout } from "./components/AppLayout";
import { DocumentDetailPage } from "./documents/DocumentDetailPage";
import { DocumentsPage } from "./documents/DocumentsPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { KnowledgeDocumentPage } from "./knowledge/KnowledgeDocumentPage";
import { KnowledgePage } from "./knowledge/KnowledgePage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import { ReviewQueuePage } from "./review/ReviewQueuePage";
import { RulesPage } from "./rules/RulesPage";
import { SearchPage } from "./search/SearchPage";

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
          { path: "/reviews", element: <ReviewQueuePage /> },
          { path: "/comparisons/:comparisonId", element: <ComparisonPage /> },
          { path: "/rules", element: <RulesPage /> },
          { path: "/search", element: <SearchPage /> },
          { path: "/knowledge", element: <KnowledgePage /> },
          { path: "/knowledge/:documentId", element: <KnowledgeDocumentPage /> },
          { path: "/status", element: <SystemStatusPage /> },
        ],
      },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
];
