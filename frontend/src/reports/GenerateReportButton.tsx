import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { Report, ReportType } from "../lib/types";

/** Generates a report of `subjectId` and opens it (only for users with reports:create). */
export function GenerateReportButton({ reportType, subjectId }: { reportType: ReportType; subjectId: string }) {
  const { token, user } = useAuth();
  const navigate = useNavigate();
  const generate = useMutation({
    mutationFn: () =>
      apiRequest<Report>("/api/v1/reports", {
        method: "POST",
        token,
        body: { report_type: reportType, subject_id: subjectId },
      }),
    onSuccess: (report) => {
      void navigate(`/reports/${report.id}`);
    },
  });
  if (!user?.permissions.includes("reports:create")) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button
        type="button"
        disabled={generate.isPending}
        onClick={() => {
          generate.mutate();
        }}
        className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50"
      >
        Generate report
      </button>
      {generate.error && (
        <span role="alert" className="text-sm text-red-700">
          {generate.error instanceof ApiError
            ? (generate.error.problem?.detail ?? generate.error.message)
            : "The report could not be generated."}
        </span>
      )}
    </span>
  );
}
