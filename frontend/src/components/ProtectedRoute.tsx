import { Navigate, Outlet, useLocation } from "react-router";

import { useAuth } from "../auth/useAuth";

export function ProtectedRoute() {
  const { status, retry, logout } = useAuth();
  const location = useLocation();

  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (status === "error") {
    return (
      <div role="alert" className="mx-auto mt-24 max-w-md rounded-lg border border-red-200 bg-white p-6">
        <p className="font-medium text-red-700">Could not load your session.</p>
        <p className="mt-1 text-sm text-slate-600">The API may be unavailable.</p>
        <div className="mt-4 flex gap-3">
          <button type="button" onClick={retry} className="rounded-md bg-blue-900 px-3 py-1.5 text-sm text-white">
            Retry
          </button>
          <button type="button" onClick={logout} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            Sign out
          </button>
        </div>
      </div>
    );
  }
  if (status === "loading") {
    return (
      <p role="status" className="mt-24 text-center text-sm text-slate-500">
        Loading session…
      </p>
    );
  }
  return <Outlet />;
}
