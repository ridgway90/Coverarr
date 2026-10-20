import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Search, SearchX } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  EmptyState,
  PageHeader,
  Pagination,
  Poster,
  PresenceChips,
  Spinner,
  inputClass,
  selectClass,
  useDebounced,
} from "../components/ui";
import { api, qs, type ItemSummary, type Page } from "../lib/api";
import { useLibraries, useServerMap } from "../lib/hooks";
import { useState } from "react";

export const ISSUE_SHORT: Record<string, string> = {
  poster_differs: "Posters differ",
  item_missing: "Missing on a server",
  push_pending: "Not pushed yet",
};

export default function LibraryPage() {
  const { libraryId } = useParams();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get("q") ?? "");
  const q = useDebounced(search);
  const filter = params.get("filter") ?? "";
  const page = Number(params.get("page") ?? 1);
  const servers = useServerMap();
  const { data: libs } = useLibraries();
  const library = libs?.libraries.find((l) => String(l.id) === libraryId);
  const expected = library?.server_libraries.filter((sl) => sl.enabled).map((sl) => sl.server_id) ?? [];

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  const missingOn = filter.startsWith("missing:") ? Number(filter.split(":")[1]) : undefined;
  const issues = ["poster", "push", "missing", "any"].includes(filter) ? filter : undefined;
  const { data, isLoading, isFetching } = useQuery({
    queryKey: ["items", libraryId, q, filter, page],
    queryFn: () =>
      api.get<Page<ItemSummary>>(
        `/api/items${qs({ library_id: libraryId, q, missing_on: missingOn, issues, page, page_size: 60 })}`,
      ),
    placeholderData: keepPreviousData,
  });

  return (
    <>
      <PageHeader
        title={library?.name ?? "Library"}
        subtitle={data ? `${data.total.toLocaleString()} ${library?.kind === "show" ? "shows" : "movies"}` : undefined}
      />
      <div className="mb-5 flex flex-wrap gap-3">
        <div className="relative min-w-60 flex-1 sm:max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <input
            className={inputClass + " pl-9"}
            placeholder="Search titles or TMDb id"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setParam("page", "");
            }}
          />
        </div>
        <select className={selectClass} value={filter} onChange={(e) => setParam("filter", e.target.value)}>
          <option value="">All titles</option>
          <option value="any">Has differences</option>
          <option value="poster">Posters differ</option>
          <option value="push">Chosen poster not pushed yet</option>
          <option value="missing">Missing on a server</option>
          {expected.map((id) => (
            <option key={id} value={`missing:${id}`}>
              Not on {servers.get(id)?.name ?? `server ${id}`}
            </option>
          ))}
        </select>
      </div>

      {!!data?.unscanned && ["any", "poster", "push"].includes(filter) && (
        <p className="mb-4 rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-sm">
          {data.unscanned.toLocaleString()} poster{data.unscanned === 1 ? " hasn't" : "s haven't"} been fingerprinted
          yet, so {data.unscanned === 1 ? "it isn't" : "they aren't"} compared. Run <strong>Scan posters</strong> from
          the dashboard (imports now do this automatically).
        </p>
      )}
      {isLoading ? (
        <Spinner />
      ) : !data?.items.length ? (
        <EmptyState icon={<SearchX className="size-8" />} title="Nothing here">
          {q || filter ? "No titles match these filters." : "Run an import from the dashboard to load this library."}
        </EmptyState>
      ) : (
        <div className={isFetching ? "opacity-70 transition-opacity" : ""}>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 2xl:grid-cols-8">
            {data.items.map((item) => (
              <Link key={item.id} to={`/items/${item.id}`} className="group">
                <div className="relative">
                  <Poster presence={item.poster} alt={item.title} className="ring-accent/0 transition group-hover:ring-2 group-hover:ring-accent" />
                  {item.issues.length > 0 && (
                    <span
                      className="absolute right-1.5 top-1.5 rounded bg-warn px-1.5 py-0.5 text-[10px] font-bold text-black"
                      title={item.issues.map((i) => ISSUE_SHORT[i] ?? i).join(", ")}
                    >
                      {item.issues.length > 1 ? `${item.issues.length} issues` : ISSUE_SHORT[item.issues[0]]}
                    </span>
                  )}
                </div>
                <div className="mt-2 truncate text-sm font-medium" title={item.title}>
                  {item.title}
                </div>
                <div className="mt-0.5 flex items-center justify-between gap-2 text-xs text-muted">
                  <span>{item.year ?? "—"}</span>
                  <PresenceChips servers={servers} presentIds={item.servers.map((s) => s.server_id)} expectedIds={expected} />
                </div>
              </Link>
            ))}
          </div>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={(p) => setParam("page", String(p))} />
        </div>
      )}
    </>
  );
}
