import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/useAuth";

// Only screens that exist are listed, and only to roles that may use them (the API enforces it
// either way); feature screens are added in their phases.
const NAV_ITEMS: { to: string; label: string; permission?: string }[] = [
  { to: "/documents", label: "Documents" },
  { to: "/search", label: "Search", permission: "documents:read" },
  { to: "/knowledge", label: "Knowledge", permission: "knowledge:read" },
  { to: "/analysis", label: "AI analysis", permission: "analysis:read" },
  { to: "/reviews", label: "Review queue", permission: "reviews:work" },
  { to: "/rules", label: "Rules", permission: "rules:read" },
  { to: "/status", label: "System status" },
  { to: "/settings/tokens", label: "API tokens" },
];

export function AppLayout() {
  const { user, logout } = useAuth();

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-8">
            <span className="text-base font-semibold text-blue-950">Document Intelligence</span>
            <nav aria-label="Main" className="flex gap-1">
              {NAV_ITEMS.filter((item) => !item.permission || user?.permissions.includes(item.permission)).map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `rounded-md px-3 py-1.5 text-sm ${
                      isActive ? "bg-blue-50 font-medium text-blue-900" : "text-slate-600 hover:bg-slate-100"
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          {user && (
            <div className="flex items-center gap-4">
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
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
