import type { OrderStatus } from "@/lib/types";

/** today.modules.orders */
export interface OrdersToday {
  count: number;
  value_aed: string;
  deliveries_due: number;
  unscheduled: number;
  by_day: { day: string; orders: number; value_aed: string }[];
}

/** contact.modules.orders */
export interface OrdersPanel {
  lifetime_orders: number;
  last_order_at: string | null;
  orders: {
    id: string;
    order_no: string;
    status: OrderStatus;
    total_aed: string | null;
    created_at: string;
    delivery_date: string | null;
  }[];
}
