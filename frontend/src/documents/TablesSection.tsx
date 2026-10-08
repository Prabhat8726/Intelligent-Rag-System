import { useQuery } from "@tanstack/react-query";

import { useAuth } from "../auth/useAuth";
import { apiRequest } from "../lib/api";
import type { DocumentTable } from "../lib/types";
import { formatPercent } from "./format";

export function TablesSection({ documentId }: { documentId: string }) {
  const { token } = useAuth();
  const tables = useQuery({
    queryKey: ["documents", "tables", documentId],
    queryFn: ({ signal }) => apiRequest<DocumentTable[]>(`/api/v1/documents/${documentId}/tables`, { token, signal }),
  });

  return (
    <section aria-labelledby="tables-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <h2 id="tables-heading" className="font-medium">
        Tables
      </h2>
      {tables.isPending && <p className="mt-2 text-sm text-slate-500">Loading tables…</p>}
      {tables.isError && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          Tables could not be loaded.
        </p>
      )}
      {tables.data?.length === 0 && <p className="mt-2 text-sm text-slate-500">No tables detected.</p>}
      {tables.data?.map((table) => (
        <div key={table.id} className="mt-4">
          <p className="text-sm text-slate-600">
            Table {table.table_index + 1} · {table.row_count} rows · page
            {table.page_start === table.page_end
              ? ` ${String(table.page_start)}`
              : `s ${String(table.page_start)}–${String(table.page_end)}`}{" "}
            · {table.extraction_method === "NATIVE" ? "text layer" : table.extraction_method.toLowerCase()} · layout
            confidence {formatPercent(table.confidence)}
          </p>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-slate-500">
                <tr>
                  {table.header.map((cell, index) => (
                    <th key={`${String(index)}-${cell}`} className="border-b border-slate-200 px-2 py-1.5 font-medium">
                      {cell}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {table.rows.map((row) => (
                  <tr key={row.row_index}>
                    {row.cells.map((cell, index) => (
                      <td key={`${String(row.row_index)}-${String(index)}`} className="px-2 py-1 tabular-nums">
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </section>
  );
}
