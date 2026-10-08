import type { DocumentStatus } from "../lib/types";
import { statusLabel, statusStyle } from "./format";

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${statusStyle(status)}`}>
      {statusLabel(status)}
    </span>
  );
}
