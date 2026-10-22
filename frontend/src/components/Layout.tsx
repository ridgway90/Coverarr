import { useQueryClient } from "@tanstack/react-query";
import { Film, FolderKanban, Layers, LayoutDashboard, ListChecks, ListOrdered, Loader2, LogOut, Menu, Settings, Sparkles, Tv, Upload, Workflow, X } from "lucide-react";
import { useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../lib/api";
import { useNavTrail } from "../lib/navTrail";
import { jobLabel, useActiveJobs, useChangesSummary, useLibraries } from "../lib/hooks";
import { CountBadge, cx } from "./ui";

function NavItem({ to, icon, label, badge }: { to: string; icon: React.ReactNode; label: string; badge?: number }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        cx(
          "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
          isActive ? "bg-accent/15 text-accent" : "text-muted hover:bg-panel-2 hover:text-text",
        )
      }
    >
      {icon}
      <span className="flex-1 truncate">{label}</span>
      {badge ? <CountBadge n={badge} /> : null}
    </NavLink>
  );
}

export default function Layout({ username }: { username: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  useNavTrail(mainRef);
  const { data: libs } = useLibraries();
  const { active, dashboard } = useActiveJobs();
  const { data: changes } = useChangesSummary();
  const pending = (changes?.titles ?? 0) + (changes?.collections ?? 0) + (changes?.problems ?? 0) + (changes?.backgrounds ?? 0);
  const openIssues = Object.values(dashboard?.issues ?? {}).reduce((a, b) => a + (b ?? 0), 0);
  const running = active.find((j) => j.status === "running") ?? active[0];

  const logout = async () => {
    await api.post("/api/auth/logout");
    qc.clear();
    qc.invalidateQueries({ queryKey: ["auth"] });
  };

  const sidebar = (
    <nav className="flex h-full flex-col gap-1 p-3" onClick={() => setOpen(false)}>
      <Link to="/" className="mb-4 flex items-center gap-2 px-3 pt-2">
        <img src="/favicon.svg" alt="" className="size-7" />
        <span className="text-lg font-bold tracking-tight">Coverarr</span>
      </Link>
      <NavItem to="/" icon={<LayoutDashboard className="size-4" />} label="Dashboard" />
      <div className="mt-3 px-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted/70">Libraries</div>
      {(libs?.libraries ?? []).map((lib) => (
        <NavItem
          key={lib.id}
          to={`/library/${lib.id}`}
          icon={lib.kind === "movie" ? <Film className="size-4" /> : <Tv className="size-4" />}
          label={lib.name}
        />
      ))}
      <NavItem to="/collections" icon={<FolderKanban className="size-4" />} label="Collections" />
      <NavItem to="/lists" icon={<ListOrdered className="size-4" />} label="List collections" />
      <div className="mt-3 px-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted/70">Manage</div>
      <NavItem to="/review" icon={<ListChecks className="size-4" />} label="Review" badge={openIssues} />
      <NavItem to="/changes" icon={<Upload className="size-4" />} label="Changes" badge={pending} />
      <NavItem to="/overlays" icon={<Layers className="size-4" />} label="Overlays" />
      <NavItem to="/housekeeping" icon={<Sparkles className="size-4" />} label="Housekeeping" />
      <NavItem to="/jobs" icon={<Workflow className="size-4" />} label="Activity" />
      <NavItem to="/settings" icon={<Settings className="size-4" />} label="Settings" />
      <div className="mt-auto space-y-2">
        {running && (
          <Link to="/jobs" className="block rounded-md border border-line bg-panel-2 p-3 text-xs">
            <div className="flex items-center gap-2 font-medium">
              <Loader2 className="size-3.5 animate-spin text-accent" />
              {jobLabel(running)}
            </div>
            <div className="mt-1 truncate text-muted">{running.message ?? "Queued"}</div>
            <div className="mt-2 h-1 overflow-hidden rounded bg-line">
              <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(running.progress * 100)}%` }} />
            </div>
          </Link>
        )}
        <div className="flex items-center justify-between px-3 py-2 text-xs text-muted">
          <span className="truncate">{username}</span>
          <button onClick={logout} className="flex items-center gap-1 hover:text-text" title="Sign out">
            <LogOut className="size-3.5" /> Sign out
          </button>
        </div>
      </div>
    </nav>
  );

  return (
    <div className="flex h-full">
      <aside className="hidden w-60 shrink-0 border-r border-line bg-panel md:block overflow-y-auto scrollbar-thin">{sidebar}</aside>
      {open && (
        <div className="fixed inset-0 z-40 bg-black/60 md:hidden" onClick={() => setOpen(false)}>
          <aside className="h-full w-64 border-r border-line bg-panel" onClick={(e) => e.stopPropagation()}>
            {sidebar}
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line bg-panel px-4 py-3 md:hidden">
          <button onClick={() => setOpen(true)} aria-label="Open menu">
            {open ? <X className="size-5" /> : <Menu className="size-5" />}
          </button>
          <span className="font-semibold">Coverarr</span>
        </header>
        <main ref={mainRef} key={location.pathname} className="flex-1 overflow-y-auto scrollbar-thin">
          <div className="mx-auto max-w-[1600px] px-4 py-6 sm:px-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
