import { ImageOff, Loader2, X } from "lucide-react";
import { useEffect, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { posterUrl, type Presence, type Server, type ServerKind } from "../lib/api";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary",
  size = "md",
  loading,
  icon,
  className,
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: "sm" | "md";
  loading?: boolean;
  icon?: ReactNode;
}) {
  const styles: Record<Variant, string> = {
    primary: "bg-accent text-black hover:bg-accent-strong",
    secondary: "bg-panel-2 text-text hover:bg-line border border-line",
    ghost: "text-muted hover:text-text hover:bg-panel-2",
    danger: "bg-danger/15 text-danger hover:bg-danger/25 border border-danger/30",
  };
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || loading}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap",
        size === "sm" ? "h-8 px-3 text-sm" : "h-10 px-4 text-sm",
        styles[variant],
        className,
      )}
    >
      {loading ? <Loader2 className="size-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cx("rounded-lg border border-line bg-panel", className)}>{children}</div>;
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-10 text-muted justify-center">
      <Loader2 className="size-5 animate-spin" />
      {label && <span className="text-sm">{label}</span>}
    </div>
  );
}

export function EmptyState({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line px-6 py-14 text-center">
      {icon && <div className="mb-3 text-muted">{icon}</div>}
      <p className="font-medium">{title}</p>
      {children && <div className="mt-2 max-w-md text-sm text-muted">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
      {error instanceof Error ? error.message : String(error)}
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:p-10" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cx("w-full rounded-lg border border-line bg-panel shadow-2xl", wide ? "max-w-3xl" : "max-w-lg")}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="font-semibold">{title}</h2>
          <button className="text-muted hover:text-text" onClick={onClose} aria-label="Close">
            <X className="size-5" />
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

const fieldBase =
  "h-10 rounded-md border border-line bg-bg px-3 text-sm text-text placeholder:text-muted/70 focus:border-accent focus:outline-none";
export const inputClass = `${fieldBase} w-full`;
export const selectClass = `${fieldBase} max-w-full`;

export const KIND_LABEL: Record<ServerKind, string> = { plex: "Plex", jellyfin: "Jellyfin", emby: "Emby" };
const KIND_COLOR: Record<ServerKind, string> = {
  plex: "bg-plex text-black",
  jellyfin: "bg-jellyfin text-white",
  emby: "bg-emby text-black",
};

export function ServerChip({ server, dim, title }: { server: Pick<Server, "kind" | "name">; dim?: boolean; title?: string }) {
  return (
    <span
      title={title ?? server.name}
      className={cx(
        "inline-flex h-5 min-w-5 items-center justify-center rounded px-1 text-[10px] font-bold uppercase leading-none",
        dim ? "bg-panel-2 text-muted line-through decoration-danger/80" : KIND_COLOR[server.kind],
      )}
    >
      {server.kind[0]}
    </span>
  );
}

/** One chip per server: coloured when present, struck through when missing. */
export function PresenceChips({
  servers,
  presentIds,
  expectedIds,
}: {
  servers: Map<number, Server>;
  presentIds: number[];
  expectedIds?: number[];
}) {
  const ids = expectedIds ?? [...servers.keys()];
  return (
    <span className="inline-flex gap-1">
      {ids.map((id) => {
        const s = servers.get(id);
        if (!s) return null;
        const present = presentIds.includes(id);
        return <ServerChip key={id} server={s} dim={!present} title={`${s.name}: ${present ? "present" : "missing"}`} />;
      })}
    </span>
  );
}

export function Poster({
  presence,
  alt,
  width = 300,
  className,
}: {
  presence: Presence | null | undefined;
  alt: string;
  width?: number;
  className?: string;
}) {
  const url = posterUrl(presence, width);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  return (
    <div className={cx("relative aspect-[2/3] overflow-hidden rounded-md bg-panel-2", className)}>
      {url && !failed ? (
        <img src={url} alt={alt} loading="lazy" className="h-full w-full object-cover" onError={() => setFailed(true)} />
      ) : (
        <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-2 text-center text-xs text-muted">
          <ImageOff className="size-6" />
          <span className="line-clamp-3">{alt}</span>
        </div>
      )}
    </div>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (p: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;
  return (
    <div className="mt-6 flex items-center justify-center gap-3 text-sm">
      <Button size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </Button>
      <span className="text-muted">
        Page {page} of {pages}
      </span>
      <Button size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next
      </Button>
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: ReactNode }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="mb-5 flex gap-1 overflow-x-auto border-b border-line scrollbar-thin" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={cx(
            "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors",
            value === t.id ? "border-accent text-text" : "border-transparent text-muted hover:text-text",
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function CountBadge({ n, tone = "accent" }: { n: number; tone?: "accent" | "muted" }) {
  if (!n) return null;
  return (
    <span
      className={cx(
        "ml-1.5 rounded-full px-1.5 py-0.5 text-[11px] font-semibold",
        tone === "accent" ? "bg-accent/20 text-accent" : "bg-panel-2 text-muted",
      )}
    >
      {n.toLocaleString()}
    </span>
  );
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "never";
  const d = new Date(value);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}
