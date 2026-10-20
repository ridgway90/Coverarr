import { useQuery } from "@tanstack/react-query";
import { FolderX, Search } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, PageHeader, Poster, PresenceChips, Spinner, Tabs, inputClass, useDebounced } from "../components/ui";
import { api, qs, type CollectionSummary } from "../lib/api";
import { useLibraries, useServerMap } from "../lib/hooks";

export const COLLECTION_ISSUE_SHORT: Record<string, string> = {
  collection_missing: "Missing on a server",
  collection_members_differ: "Members differ",
  poster_differs: "Posters differ",
};

export default function CollectionsPage() {
  const [params, setParams] = useSearchParams();
  const { data: libs } = useLibraries();
  const servers = useServerMap();
  const [search, setSearch] = useState("");
  const q = useDebounced(search);
  const libraries = libs?.libraries ?? [];
  const libraryId = Number(params.get("library") ?? libraries[0]?.id ?? 0);
  const issuesOnly = params.get("issues") === "1";
  const library = libraries.find((l) => l.id === libraryId);
  const expected = library?.server_libraries.filter((s) => s.enabled).map((s) => s.server_id) ?? [];

  const { data, isLoading } = useQuery({
    queryKey: ["collections", libraryId, q, issuesOnly],
    queryFn: () =>
      api.get<CollectionSummary[]>(`/api/collections${qs({ library_id: libraryId, q, issues_only: issuesOnly || undefined })}`),
    enabled: libraryId > 0,
  });

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageHeader title="Collections" subtitle={data ? `${data.length} collections` : undefined} />
      {libraries.length > 1 && (
        <Tabs
          value={String(libraryId)}
          onChange={(id) => update("library", id)}
          tabs={libraries.map((l) => ({ id: String(l.id), label: l.name }))}
        />
      )}
      <div className="mb-5 flex flex-wrap items-center gap-4">
        <div className="relative min-w-60 flex-1 sm:max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <input className={inputClass + " pl-9"} placeholder="Search collections" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={issuesOnly} onChange={(e) => update("issues", e.target.checked ? "1" : "")} />
          Only with differences
        </label>
      </div>
      {isLoading ? (
        <Spinner />
      ) : !data?.length ? (
        <EmptyState icon={<FolderX className="size-8" />} title="No collections">
          {q || issuesOnly ? "Nothing matches these filters." : "Collections show up here after an import."}
        </EmptyState>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 2xl:grid-cols-8">
          {data.map((c) => {
            const counts = c.servers.map((s) => s.member_count);
            return (
              <Link key={c.id} to={`/collections/${c.id}`} className="group">
                <div className="relative">
                  <Poster presence={c.poster} alt={c.name} className="transition group-hover:ring-2 group-hover:ring-accent" />
                  {c.issues.length > 0 && (
                    <span className="absolute right-1.5 top-1.5 rounded bg-warn px-1.5 py-0.5 text-[10px] font-bold text-black">
                      {c.issues.length > 1 ? `${c.issues.length} issues` : COLLECTION_ISSUE_SHORT[c.issues[0]]}
                    </span>
                  )}
                </div>
                <div className="mt-2 truncate text-sm font-medium" title={c.name}>
                  {c.name}
                </div>
                <div className="mt-0.5 flex items-center justify-between gap-2 text-xs text-muted">
                  <span>{counts.length ? `${Math.max(...counts)} items` : ""}</span>
                  <PresenceChips servers={servers} presentIds={c.servers.map((s) => s.server_id)} expectedIds={expected} />
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </>
  );
}
