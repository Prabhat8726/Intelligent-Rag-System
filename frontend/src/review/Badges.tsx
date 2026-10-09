import type { ItemStatus, ReviewPriority, RuleOutcome } from "../lib/types";
import { ITEM_STATUS_STYLES, OUTCOME_LABELS, OUTCOME_STYLES, PRIORITY_LABELS, PRIORITY_STYLES } from "./format";

function Badge({ text, style }: { text: string; style: string }) {
  return <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${style}`}>{text}</span>;
}

export function ItemStatusBadge({ status }: { status: ItemStatus }) {
  return <Badge text={status} style={ITEM_STATUS_STYLES[status]} />;
}

export function OutcomeBadge({ outcome }: { outcome: RuleOutcome }) {
  return <Badge text={OUTCOME_LABELS[outcome]} style={OUTCOME_STYLES[outcome]} />;
}

export function PriorityBadge({ priority }: { priority: ReviewPriority }) {
  return <Badge text={PRIORITY_LABELS[priority]} style={PRIORITY_STYLES[priority]} />;
}
