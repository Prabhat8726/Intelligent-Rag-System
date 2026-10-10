import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/useAuth";
import { apiRequest } from "../lib/api";
import type { WorkflowCounts } from "../lib/types";

interface NavItem {
  to: string;
  label: string;
  permission?: string;
}

// Only screens that exist are listed, and only to roles that may use them (the API enforces it
// either way). Day-to-day work in the main navigation; settings and administration below it.
const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", permission: "dashboard:read" },
  { to: "/documents", label: "Documents" },
  { to: "/search", label: "Search", permission: "documents:read" },
  { to: "/reviews", label: "Review queue", permission: "reviews:work" },
  { to: "/workflows", label: "Workflows", permission: "workflows:read" },
  { to: "/analysis", label: "AI analysis", permission: "analysis:read" },
  { to: "/knowledge", label: "Knowledge", permission: "knowledge:read" },
  { to: "/reports", label: "Reports", permission: "reports:read" },
  { to: "/rules", label: "Rules", permission: "rules:read" },
];
const SETTINGS_ITEMS: NavItem[] = [
  { to: "/settings/tokens", label: "API tokens" },
  { to: "/admin/audit", label: "Audit log", permission: "audit:read" },
  { to: "/admin/users", label: "Users", permission: "users:manage" },
  { to: "/status", label: "System status" },
];
const COUNT_REFRESH_MS = 30_000;

function linkClass({ isActive }: { isActive: boolean }): string {
  return `rounded-md px-3 py-1.5 text-sm ${
    isActive ? "bg-blue-50 font-medium text-blue-900" : "text-slate-600 hover:bg-slate-100"
  }`;
}

export function AppLayout() {
  const { user, token, logout } = useAuth();
  const allowed = (item: NavItem) => !item.permission || (user?.permissions.includes(item.permission) ?? false);
  const mayDecide = user?.permissions.includes("workflows:approve") ?? false;
  const counts = useQuery({
    queryKey: ["workflow-counts"],
    queryFn: () => apiRequest<WorkflowCounts>("/api/v1/workflows/summary", { token }),
    enabled: mayDecide,
    refetchInterval: COUNT_REFRESH_MS,
  });
  const waiting = counts.data?.awaiting_my_decision ?? 0;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-6 py-3">
          <div className="flex items-center gap-6">
            <span className="shrink-0 text-base font-semibold text-blue-950">Document Intelligence</span>
            <nav aria-label="Main" className="flex flex-wrap gap-1">
              {NAV_ITEMS.filter(allowed).map((item) => (
                <NavLink key={item.to} to={item.to} className={linkClass}>
                  {item.label}
                  {item.to === "/workflows" && waiting > 0 && (
                    <span
                      aria-label={`${String(waiting)} awaiting your decision`}
                      className="ml-1.5 rounded-full bg-amber-500 px-1.5 text-xs font-semibold text-white"
                    >
                      {waiting}
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>
          </div>
          {user && (
            <div className="flex shrink-0 items-center gap-4">
              <div className="text-right text-sm leading-tight">
                <div className="font-medium">{user.full_name}</div>
                <div className="text-xs text-slate-500">
                  {user.role}
                  {user.department ? ` · ${user.department.name}` : ""}
                </div>
              </div>
              <button
                type="button"
                onClick={logout}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
              >
                Sign out
              </button>
            </div>
          )}
        </div>
        <nav aria-label="Settings" className="mx-auto flex max-w-6xl justify-end gap-1 px-6 pb-2">
          {SETTINGS_ITEMS.filter(allowed).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `rounded px-2 py-0.5 text-xs ${isActive ? "font-medium text-blue-900" : "text-slate-500 hover:text-slate-800"}`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
