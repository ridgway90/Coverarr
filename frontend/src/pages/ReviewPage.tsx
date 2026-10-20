import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, EyeOff, FolderPlus, Image, Layers, RotateCcw, Search, UserPlus } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Button,
  Card,
  CountBadge,
  EmptyState,
  ErrorNote,
  PageHeader,
  Pagination,
  Poster,
  PresenceChips,
  ServerChip,
  Spinner,
  Tabs,
  inputClass,
  selectClass,
  useDebounced,
} from "../components/ui";
import { api, qs, type Issue, type IssueType, type ReviewSummary, type Server } from "../lib/api";
import MismatchRow from "../components/MismatchRow";
import RenameCollection from "../components/RenameCollection";
import { useLibraries, useServerMap } from "../lib/hooks";

const TABS: { id: IssueType; label: string; help: string }[] = [
  {
    id: "mismatch",
    label: "Mismatched",
    help: "The same file is matched to different titles on different servers, so one of them is wrong. Coverarr suggests the likely match with its reasons, but nothing changes until you confirm.",
  },
  {
    id: "similar_collections",
    label: "Similar collections",
    help: "Separate collections whose names look the same, such as a film series and its wider \"universe\". Give one a clearer name so they're easy to tell apart, or ignore it if the names are fine.",
  },
  {
    id: "collection_missing",
    label: "Missing collections",
    help: "Collections that exist on some servers but not others. Creating one adds every member that exists on that server.",
  },
  {
    id: "collection_members_differ",
    label: "Collection members",
    help: "Collections on several servers whose members differ. Adding members uses every server's members combined; nothing is removed.",
  },
  {
    id: "poster_differs",
    label: "Posters",
    help: "Titles whose artwork looks different between servers (overlays such as Kometa's are ignored). Choose which server has the clean base poster.",
  },
  {
    id: "item_missing",
    label: "Missing titles",
    help: "Titles not found on every server. Usually a file missing on one server, or an item matched to the wrong TMDb/TVDB id there.",
  },
];

const names = (ids: number[], servers: Map<number, Server>) => ids.map((id) => servers.get(id)?.name ?? `#${id}`).join(", ");

export function describeIssue(issue: Issue, servers: Map<number, Server>): ReactNode {
  const d = issue.details;
  switch (issue.type) {
    case "mismatch":
      return "Matched to different titles on different servers";
    case "similar_collections":
      return "Another collection has a very similar name";
    case "item_missing":
      return `Missing on ${names(d.missing, servers)}${d.no_ids ? " (no TMDb/TVDB/IMDb ids)" : ""}`;
    case "collection_missing":
      return `Collection missing on ${names(d.missing, servers)}`;
    case "collection_members_differ":
      return Object.entries(d.missing as Record<string, { count: number }>)
        .map(([sid, v]) => `${servers.get(Number(sid))?.name ?? sid} is missing ${v.count} member${v.count === 1 ? "" : "s"}`)
        .join("; ");
    case "poster_differs":
      return d.missing_poster ? "A server has no poster" : "Posters differ between servers";
  }
}

export default function ReviewPage() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const summary = useQuery({ queryKey: ["review", "summary"], queryFn: () => api.get<ReviewSummary>("/api/review/summary") });
  // Without an explicit tab, open the first one that has something to do.
  const firstOpen = TABS.find((t) => (summary.data?.[t.id]?.open ?? 0) > 0)?.id;
  const type = (params.get("type") as IssueType) || firstOpen || TABS[0].id;
  const status = params.get("status") === "ignored" ? "ignored" : "open";
  const page = Number(params.get("page") ?? 1);
  const libraryId = params.get("library") ?? "";
  const [search, setSearch] = useState("");
  const q = useDebounced(search);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [posterServer, setPosterServer] = useState<number | "">("");
  const servers = useServerMap();
  const { data: libs } = useLibraries();

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setParams(next, { replace: true });
  };
  useEffect(() => setSelected(new Set()), [type, status, page, libraryId, q]);

  const list = useQuery({
    queryKey: ["issues", type, status, libraryId, q, page],
    enabled: summary.isFetched || params.has("type"),
    queryFn: () =>
      api.get<{ total: number; page: number; page_size: number; issues: Issue[] }>(
        `/api/review/issues${qs({ type, status, library_id: libraryId, q, page, page_size: 50 })}`,
      ),
    placeholderData: keepPreviousData,
  });

  const act = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<{ updated: number; skipped?: number; job_id?: number }>("/api/review/actions", body),
    onSuccess: () => {
      setSelected(new Set());
      for (const key of ["issues", "review", "dashboard", "items", "collections"]) qc.invalidateQueries({ queryKey: [key] });
    },
  });

  const issues = list.data?.issues ?? [];
  const tab = TABS.find((t) => t.id === type)!;
  const ids = [...selected];
  const scope = (all: boolean) =>
    all ? { all_of_type: type, library_id: libraryId ? Number(libraryId) : undefined } : { issue_ids: ids };
  const total = list.data?.total ?? 0;
  const allSelected = issues.length > 0 && issues.every((i) => selected.has(i.id));

  const toggle = (id: number) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <>
      <PageHeader title="Review" subtitle="Bring Plex, Jellyfin and Emby onto the same base before Coverarr starts managing them." />
      <Tabs<IssueType>
        value={type}
        onChange={(id) => {
          const next = new URLSearchParams();
          next.set("type", id);
          if (libraryId) next.set("library", libraryId);
          setParams(next, { replace: true });
        }}
        tabs={TABS.map((t) => ({
          id: t.id,
          label: (
            <>
              {t.label}
              <CountBadge n={summary.data?.[t.id]?.open ?? 0} />
            </>
          ),
        }))}
      />
      <p className="mb-4 max-w-3xl text-sm text-muted">{tab.help}</p>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative min-w-56 flex-1 sm:max-w-xs">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <input className={inputClass + " pl-9"} placeholder="Filter by title" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <select className={selectClass} value={libraryId} onChange={(e) => setParam("library", e.target.value)}>
          <option value="">All libraries</option>
          {libs?.libraries.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
        <select className={selectClass} value={status} onChange={(e) => setParam("status", e.target.value === "open" ? "" : e.target.value)}>
          <option value="open">Open</option>
          <option value="ignored">Ignored ({summary.data?.[type]?.ignored ?? 0})</option>
        </select>
      </div>

      {total > 0 && (
        <Card className="sticky top-0 z-10 mb-4 flex flex-wrap items-center gap-2 px-4 py-3">
          <label className="mr-2 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={() => setSelected(allSelected ? new Set() : new Set(issues.map((i) => i.id)))}
            />
            {selected.size ? `${selected.size} selected` : "Select page"}
          </label>
          <div className="flex-1" />
          {status === "open" && type === "collection_missing" && (
            <>
              <Button size="sm" icon={<FolderPlus className="size-4" />} disabled={!selected.size} loading={act.isPending} onClick={() => act.mutate({ action: "create_collections", ...scope(false) })}>
                Create selected
              </Button>
              <Button size="sm" variant="primary" icon={<FolderPlus className="size-4" />} loading={act.isPending} onClick={() => confirm(`Create ${total} collections on the servers that are missing them?`) && act.mutate({ action: "create_collections", ...scope(true) })}>
                Create all {total}
              </Button>
            </>
          )}
          {status === "open" && type === "collection_members_differ" && (
            <>
              <Button size="sm" icon={<UserPlus className="size-4" />} disabled={!selected.size} loading={act.isPending} onClick={() => act.mutate({ action: "sync_members", ...scope(false) })}>
                Add members to selected
              </Button>
              <Button size="sm" variant="primary" icon={<UserPlus className="size-4" />} loading={act.isPending} onClick={() => confirm(`Add missing members to ${total} collections?`) && act.mutate({ action: "sync_members", ...scope(true) })}>
                Fix all {total}
              </Button>
            </>
          )}
          {status === "open" && type === "poster_differs" && (
            <>
              <select className={selectClass + " h-8"} value={posterServer} onChange={(e) => setPosterServer(e.target.value ? Number(e.target.value) : "")}>
                <option value="">Use poster from…</option>
                {[...servers.values()].map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <Button size="sm" icon={<Image className="size-4" />} disabled={!selected.size || posterServer === ""} loading={act.isPending} onClick={() => act.mutate({ action: "choose_poster", server_id: posterServer, ...scope(false) })}>
                Apply to selected
              </Button>
              <Button
                size="sm"
                variant="primary"
                icon={<Image className="size-4" />}
                disabled={posterServer === ""}
                loading={act.isPending}
                onClick={() =>
                  confirm(`Use ${servers.get(Number(posterServer))?.name}'s poster as the base for all ${total} titles? Titles without a poster there are skipped.`) &&
                  act.mutate({ action: "choose_poster", server_id: posterServer, ...scope(true) })
                }
              >
                Apply to all {total}
              </Button>
            </>
          )}
          {status === "open" ? (
            <Button size="sm" variant="ghost" icon={<EyeOff className="size-4" />} disabled={!selected.size} onClick={() => act.mutate({ action: "ignore", ...scope(false) })}>
              Ignore
            </Button>
          ) : (
            <Button size="sm" variant="ghost" icon={<RotateCcw className="size-4" />} disabled={!selected.size} onClick={() => act.mutate({ action: "reopen", ...scope(false) })}>
              Reopen
            </Button>
          )}
        </Card>
      )}
      <ErrorNote error={act.error} />
      {act.data?.job_id && (
        <div className="mb-4 rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-sm">
          Pushing changes to your servers. Progress is in the sidebar, and this list refreshes when it's done.
        </div>
      )}
      {act.data?.skipped ? (
        <div className="mb-4 rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-sm">
          {act.data.skipped} skipped because that server has no poster for them.
        </div>
      ) : null}

      {list.isLoading ? (
        <Spinner />
      ) : issues.length === 0 ? (
        <EmptyState icon={<CheckCircle2 className="size-8 text-ok" />} title={status === "open" ? "Nothing to review here" : "Nothing ignored"}>
          {status === "open" ? "All servers agree on this. If you haven't yet, run an import (and a poster scan for posters)." : null}
        </EmptyState>
      ) : (
        <Card className={list.isFetching ? "opacity-70" : ""}>
          <ul className="divide-y divide-line">
            {issues.map((issue) => (
              <li key={issue.id} className="flex gap-3 px-4 py-3">
                <input type="checkbox" className="mt-1" checked={selected.has(issue.id)} onChange={() => toggle(issue.id)} aria-label={`Select ${issue.title}`} />
                <div className="min-w-0 flex-1">
                  <IssueRow issue={issue} servers={servers} onChoose={(sid) => act.mutate({ action: "choose_poster", server_id: sid, issue_ids: [issue.id] })} />
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Pagination page={page} pageSize={50} total={total} onPage={(p) => setParam("page", String(p))} />
    </>
  );
}

function IssueRow({ issue, servers, onChoose }: { issue: Issue; servers: Map<number, Server>; onChoose: (serverId: number) => void }) {
  const d = issue.details;
  const link = issue.media_item_id ? `/items/${issue.media_item_id}` : `/collections/${issue.collection_id}`;
  const title = (
    <Link to={link} className="font-medium hover:text-accent">
      {issue.title}
    </Link>
  );

  if (issue.type === "mismatch") return <MismatchRow issue={issue} servers={servers} />;
  if (issue.type === "similar_collections") return <SimilarRow issue={issue} servers={servers} />;

  if (issue.type === "poster_differs") {
    const entries = d.servers as { server_id: number; server_item_id?: number; server_collection_id?: number; has_poster: boolean; kometa_overlay: boolean }[];
    return (
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          {title}
          {d.missing_poster && <span className="text-xs text-warn">a server has no poster</span>}
        </div>
        <div className="flex flex-wrap gap-3">
          {entries.map((e) => {
            const server = servers.get(e.server_id);
            return (
              <div key={e.server_id} className="w-28">
                <Poster presence={{ ...e, poster_version: String(issue.id) }} alt={`${issue.title} on ${server?.name}`} width={200} />
                <div className="mt-1 flex items-center gap-1 text-xs">
                  {server && <ServerChip server={server} />}
                  <span className="truncate">{server?.name}</span>
                  {e.kometa_overlay && <Layers className="size-3.5 shrink-0 text-warn" aria-label="Kometa overlay" />}
                </div>
                <Button size="sm" className="mt-1 h-7 w-full text-xs" disabled={!e.has_poster} onClick={() => onChoose(e.server_id)}>
                  Use this
                </Button>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  if (issue.type === "collection_members_differ") {
    const missing = d.missing as Record<string, { count: number; items: { id: number; title: string }[] }>;
    return (
      <div>
        {title} <span className="text-sm text-muted">· {d.member_count} titles in total</span>
        <ul className="mt-1 space-y-1 text-sm">
          {Object.entries(missing).map(([sid, v]) => (
            <li key={sid} className="text-muted">
              <span className="text-text">{servers.get(Number(sid))?.name}</span> is missing {v.count}:{" "}
              {v.items.map((m, idx) => (
                <span key={m.id}>
                  {idx > 0 && ", "}
                  <Link to={`/items/${m.id}`} className="hover:text-accent">
                    {m.title}
                  </Link>
                </span>
              ))}
              {v.count > v.items.length && ` and ${v.count - v.items.length} more`}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (issue.type === "collection_missing") {
    const available = d.available as Record<string, number>;
    return (
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {title}
        <PresenceChips servers={servers} presentIds={d.present} expectedIds={[...d.present, ...d.missing].sort()} />
        <span className="text-sm text-muted">
          {d.member_count} titles ·{" "}
          {d.missing.map((sid: number) => `${available[String(sid)] ?? 0} available on ${servers.get(sid)?.name}`).join(", ")}
          {d.smart_on?.length ? ` · smart collection on ${names(d.smart_on, servers)} (created as a regular collection)` : ""}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      {title}
      <PresenceChips servers={servers} presentIds={d.present} expectedIds={[...d.present, ...d.missing].sort()} />
      <span className="text-sm text-muted">
        Missing on {names(d.missing, servers)}
        {d.no_ids && " · no TMDb/TVDB/IMDb ids on this title"}
      </span>
    </div>
  );
}

function SimilarRow({ issue, servers }: { issue: Issue; servers: Map<number, Server> }) {
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null);
  const colls = issue.details.collections as { id: number; name: string; servers: number[]; member_count: number }[];
  return (
    <div>
      <div className="text-sm text-muted">These look like the same collection but are separate:</div>
      <ul className="mt-2 space-y-2">
        {colls.map((c) => (
          <li key={c.id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Link to={`/collections/${c.id}`} className="font-medium hover:text-accent">
              {c.name}
            </Link>
            <PresenceChips servers={servers} presentIds={c.servers} expectedIds={c.servers} />
            <span className="text-sm text-muted">{c.member_count} titles</span>
            <Button size="sm" variant="ghost" onClick={() => setRenaming({ id: c.id, name: c.name })}>
              Rename…
            </Button>
          </li>
        ))}
      </ul>
      {renaming && <RenameCollection collectionId={renaming.id} currentName={renaming.name} onClose={() => setRenaming(null)} />}
    </div>
  );
}
