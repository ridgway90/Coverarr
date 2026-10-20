import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, qs } from "../lib/api";

interface Neighbours {
  prev: { id: number; title: string } | null;
  next: { id: number; title: string } | null;
  position: number | null;
  total: number;
}

/** The list a title or collection was opened from, as the API's filter parameters. */
function listFilters(kind: "items" | "collections", from: URLSearchParams) {
  if (kind === "collections") {
    return { q: from.get("q") ?? undefined, issues_only: from.get("issues") === "1" || undefined, letter: from.get("letter") ?? undefined };
  }
  const filter = from.get("filter") ?? "";
  return {
    q: from.get("q") ?? undefined,
    letter: from.get("letter") ?? undefined,
    issues: ["poster", "push", "missing", "any"].includes(filter) ? filter : undefined,
    missing_on: filter.startsWith("missing:") ? filter.split(":")[1] : undefined,
  };
}

/** Previous / next buttons (and the arrow keys) through the list this page was opened from. */
export default function PrevNext({ kind, id, from }: { kind: "items" | "collections"; id: number; from: string }) {
  const navigate = useNavigate();
  const { data } = useQuery({
    queryKey: [kind === "items" ? "item" : "collection", "neighbours", id, from],
    queryFn: () => api.get<Neighbours>(`/api/${kind}/${id}/neighbours${qs(listFilters(kind, new URLSearchParams(from)))}`),
  });
  const href = (target: { id: number } | null) => target && `/${kind}/${target.id}${from ? `?from=${encodeURIComponent(from)}` : ""}`;
  const prev = href(data?.prev ?? null);
  const next = href(data?.next ?? null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return;
      if (document.querySelector('[role="dialog"]')) return;
      if (e.key === "ArrowLeft" && prev) navigate(prev);
      if (e.key === "ArrowRight" && next) navigate(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prev, next, navigate]);

  if (!data || data.position === null) return null;
  const button = "inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-sm transition-colors";
  return (
    <div className="flex items-center gap-2" aria-label="Browse">
      {prev ? (
        <Link to={prev} className={`${button} hover:bg-panel-2`} title={`${data.prev!.title} (←)`}>
          <ChevronLeft className="size-4" /> Previous
        </Link>
      ) : (
        <span className={`${button} cursor-default opacity-40`}>
          <ChevronLeft className="size-4" /> Previous
        </span>
      )}
      <span className="text-xs text-muted">
        {data.position.toLocaleString()} of {data.total.toLocaleString()}
      </span>
      {next ? (
        <Link to={next} className={`${button} hover:bg-panel-2`} title={`${data.next!.title} (→)`}>
          Next <ChevronRight className="size-4" />
        </Link>
      ) : (
        <span className={`${button} cursor-default opacity-40`}>
          Next <ChevronRight className="size-4" />
        </span>
      )}
    </div>
  );
}
