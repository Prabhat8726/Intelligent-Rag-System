import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { AdminUser, Department, Role, UserPage } from "../lib/types";

const ROLES: Role[] = ["VIEWER", "REVIEWER", "ANALYST", "MANAGER", "ADMIN"];

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

function NewUserForm({ departments }: { departments: Department[] }) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [role, setRole] = useState<Role>("ANALYST");
  const [departmentId, setDepartmentId] = useState(departments[0]?.id ?? "");
  const [password, setPassword] = useState("");
  const create = useMutation({
    mutationFn: () =>
      apiRequest<AdminUser>("/api/v1/users", {
        method: "POST",
        token,
        body: {
          email,
          full_name: fullName,
          role,
          department_id: role === "ADMIN" ? null : departmentId || null,
          password,
        },
      }),
    onSuccess: () => {
      setEmail("");
      setFullName("");
      setPassword("");
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    create.mutate();
  };
  return (
    <form onSubmit={submit} aria-label="New user" className="grid gap-3 rounded-xl border border-slate-200 bg-white p-5 sm:grid-cols-2">
      <label className="text-sm">
        <span className="text-slate-700">Email</span>
        <input
          type="email"
          required
          value={email}
          onChange={(event) => {
            setEmail(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
      </label>
      <label className="text-sm">
        <span className="text-slate-700">Full name</span>
        <input
          required
          value={fullName}
          onChange={(event) => {
            setFullName(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
      </label>
      <label className="text-sm">
        <span className="text-slate-700">Role</span>
        <select
          value={role}
          onChange={(event) => {
            setRole(event.target.value as Role);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm"
        >
          {ROLES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm">
        <span className="text-slate-700">Department</span>
        <select
          value={role === "ADMIN" ? "" : departmentId}
          disabled={role === "ADMIN"}
          onChange={(event) => {
            setDepartmentId(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm disabled:bg-slate-100"
        >
          {role === "ADMIN" && <option value="">None (administrators see every department)</option>}
          {departments.map((department) => (
            <option key={department.id} value={department.id}>
              {department.name}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm sm:col-span-2">
        <span className="text-slate-700">Initial password (at least 12 characters)</span>
        <input
          type="password"
          required
          autoComplete="new-password"
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
      </label>
      {create.error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 sm:col-span-2">
          {errorText(create.error)}
        </p>
      )}
      <div className="sm:col-span-2">
        <button
          type="submit"
          disabled={create.isPending}
          className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
        >
          Create user
        </button>
      </div>
    </form>
  );
}

function UserRow({ item, departments, self }: { item: AdminUser; departments: Department[]; self: boolean }) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const update = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiRequest<AdminUser>(`/api/v1/users/${item.id}`, { method: "PATCH", token, body }),
    onSuccess: () => {
      setNotice(null);
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
  });
  const reset = useMutation({
    mutationFn: () =>
      apiRequest<null>(`/api/v1/users/${item.id}/password`, { method: "POST", token, body: { password } }),
    onSuccess: () => {
      setPassword("");
      setNotice("Password set; the account is unlocked.");
    },
  });
  const error = update.error ?? reset.error;
  return (
    <tr className="align-top" aria-label={item.email}>
      <td className="px-4 py-2.5">
        <div className="font-medium">{item.full_name}</div>
        <div className="text-xs text-slate-500">{item.email}</div>
        {item.locked_until && new Date(item.locked_until) > new Date() && (
          <div className="text-xs text-red-700">locked until {new Date(item.locked_until).toLocaleTimeString()}</div>
        )}
      </td>
      <td className="py-2.5">
        <select
          aria-label={`Role of ${item.email}`}
          value={item.role}
          disabled={self}
          onChange={(event) => {
            const role = event.target.value as Role;
            update.mutate(
              role === "ADMIN"
                ? { role }
                : { role, department_id: item.department?.id ?? departments[0]?.id ?? null },
            );
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm disabled:bg-slate-100"
        >
          {ROLES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </td>
      <td className="py-2.5">
        <select
          aria-label={`Department of ${item.email}`}
          value={item.department?.id ?? ""}
          onChange={(event) => {
            update.mutate({ department_id: event.target.value || null });
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
        >
          {item.role === "ADMIN" && <option value="">None</option>}
          {departments.map((department) => (
            <option key={department.id} value={department.id}>
              {department.name}
            </option>
          ))}
        </select>
      </td>
      <td className="py-2.5">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={item.is_active}
            disabled={self}
            onChange={(event) => {
              update.mutate({ is_active: event.target.checked });
            }}
          />
          active
        </label>
      </td>
      <td className="py-2.5 pr-4">
        <div className="flex gap-2">
          <input
            type="password"
            aria-label={`New password for ${item.email}`}
            autoComplete="new-password"
            placeholder="new password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
            className="w-36 rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
          <button
            type="button"
            disabled={password.length === 0 || reset.isPending}
            onClick={() => {
              reset.mutate();
            }}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100 disabled:opacity-50"
          >
            Set
          </button>
        </div>
        {notice && <p className="mt-1 text-xs text-emerald-700">{notice}</p>}
        {error && (
          <p role="alert" className="mt-1 text-xs text-red-700">
            {errorText(error)}
          </p>
        )}
      </td>
    </tr>
  );
}

export function UsersPage() {
  const { token, user } = useAuth();
  const [query, setQuery] = useState("");
  const departments = useQuery({
    queryKey: ["departments"],
    queryFn: () => apiRequest<Department[]>("/api/v1/departments", { token }),
  });
  const users = useQuery({
    queryKey: ["users", query],
    queryFn: () =>
      apiRequest<UserPage>(`/api/v1/users?${new URLSearchParams({ q: query, limit: "200" }).toString()}`, { token }),
  });
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Users</h1>
        <p className="mt-1 text-sm text-slate-600">
          Roles decide what people may do; departments decide which documents they see. Changes take effect on the
          next request. You cannot change your own role or deactivate yourself, and one active administrator always
          remains. Deactivating someone revokes their API tokens.
        </p>
      </div>
      {departments.data && <NewUserForm departments={departments.data} />}
      <input
        type="search"
        aria-label="Find users"
        placeholder="Find by email or name"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
        }}
        className="w-72 rounded-md border border-slate-300 px-3 py-2 text-sm"
      />
      <section aria-label="User list" className="rounded-xl border border-slate-200 bg-white">
        {users.isPending || !departments.data ? (
          <p className="p-5 text-sm text-slate-500">Loading…</p>
        ) : users.isError ? (
          <p role="alert" className="p-5 text-sm text-red-700">
            {errorText(users.error)}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-medium">User</th>
                  <th className="py-2 font-medium">Role</th>
                  <th className="py-2 font-medium">Department</th>
                  <th className="py-2 font-medium">State</th>
                  <th className="py-2 pr-4 font-medium">Password</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {users.data.items.map((item) => (
                  <UserRow key={item.id} item={item} departments={departments.data} self={item.id === user?.id} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
