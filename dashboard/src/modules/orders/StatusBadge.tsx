import { Badge } from "@/components/ui/badge";
import { label, STATUS_LABEL } from "@/lib/format";
import type { OrderStatus } from "@/lib/types";

const STATUS_TONE: Record<OrderStatus, "neutral" | "accent" | "good" | "warn" | "bad"> = {
  draft: "neutral",
  confirmed: "accent",
  out_for_delivery: "warn",
  delivered: "good",
  cancelled: "bad",
};

export function StatusBadge({ status }: { status: OrderStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{label(STATUS_LABEL, status)}</Badge>;
}
