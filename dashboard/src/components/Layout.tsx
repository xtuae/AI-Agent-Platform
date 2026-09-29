import { Home, LogOut, MessagesSquare, Package, Settings, Users } from "lucide-react";
import { Suspense } from "react";
import { NavLink, Outlet } from "react-router";
import { logout } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useLive } from "@/lib/stream";
import { cn } from "@/lib/utils";
import { Spinner } from "./ui/misc";

const NAV = [
  { to: "/", label: "Today", icon: Home, end: true },
  { to: "/orders", label: "Orders", icon: Package, end: false },
  { to: "/conversations", label: "Chats", icon: MessagesSquare, end: false },
  { to: "/customers", label: "Customers", icon: Users, end: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false },
] as const;

function LiveBadge() {
  const live = useLive();
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted" title={live ? "Updates arrive as they happen" : "Refreshing every 15 seconds"}>
      <span className={cn("size-2 rounded-full", live ? "bg-[var(--dot-good)]" : "bg-line")} aria-hidden />
      {live ? "Live" : "Every 15 s"}
    </span>
  );
}

export function Layout() {
  const { session } = useAuth();
  return (
    <div className="min-h-dvh md:flex">
      {/* desktop sidebar */}
      <aside className="no-print hidden w-56 shrink-0 flex-col border-r border-line bg-surface md:flex">
        <div className="px-4 py-5">
          <p className="truncate text-sm font-semibold">{session?.tenant.name}</p>
          <p className="truncate text-xs text-muted">{session?.user.name ?? session?.user.email}</p>
        </div>
        <nav className="flex-1 space-y-0.5 px-2" aria-label="Main">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm",
                  isActive ? "bg-accent/10 font-medium text-accent-ink" : "text-ink-2 hover:bg-line/50",
                )
              }
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-3 border-t border-line p-4">
          <LiveBadge />
          <button onClick={() => void logout()} className="flex items-center gap-2 text-sm text-ink-2 hover:text-ink">
            <LogOut className="size-4" aria-hidden /> Sign out
          </button>
        </div>
      </aside>

      <div className="min-w-0 flex-1">
        {/* phone header */}
        <header className="no-print sticky top-0 z-30 flex items-center justify-between border-b border-line bg-page/95 px-4 py-3 backdrop-blur md:hidden">
          <p className="truncate text-sm font-semibold">{session?.tenant.name}</p>
          <LiveBadge />
        </header>
        <main className="pb-nav mx-auto w-full max-w-6xl px-4 pt-4 md:px-8 md:pt-8">
          <Suspense
            fallback={
              <div className="flex justify-center py-16">
                <Spinner />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>
      </div>

      {/* phone bottom nav */}
      <nav
        aria-label="Main"
        className="no-print fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-line bg-surface pb-[env(safe-area-inset-bottom)] md:hidden"
      >
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                "flex min-h-14 flex-col items-center justify-center gap-0.5 text-[11px]",
                isActive ? "font-medium text-accent-ink" : "text-muted",
              )
            }
          >
            <Icon className="size-5" aria-hidden />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
