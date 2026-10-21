import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import PrevNext from "../components/PrevNext";
import PosterChooser from "../components/PosterChooser";
import PendingPush from "../components/PendingPush";
import { Button, Card, ErrorNote, PageHeader, Poster, PresenceChips, Spinner, cx } from "../components/ui";
import { api, type ItemDetail } from "../lib/api";
import { useJobAction, useLibraries, useServerMap } from "../lib/hooks";
import { useBack } from "../lib/navTrail";
import { describeIssue } from "./ReviewPage";

export default function ItemDetailPage() {
  const { itemId } = useParams();
  const [search] = useSearchParams();
  const from = search.get("from") ?? "";
  const qc = useQueryClient();
  const servers = useServerMap();
  const { data: libs } = useLibraries();
  const { data: item, isLoading, error } = useQuery({
    queryKey: ["item", itemId],
    queryFn: () => api.get<ItemDetail>(`/api/items/${itemId}`),
  });
  // A season is refreshed with its show (servers re-read seasons and episodes together).
  const reload = useJobAction(() => api.post<{ job_id: number }>(`/api/items/${item?.parent?.id ?? itemId}/refresh`), ["item", "items"]);
  const choose = useMutation({
    mutationFn: (source: string | null) => api.put(`/api/items/${itemId}/poster-source`, { poster_source: source }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["item", itemId] });
      qc.invalidateQueries({ queryKey: ["items"] });
      qc.invalidateQueries({ queryKey: ["issues"] });
      qc.invalidateQueries({ queryKey: ["review"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["changes"] });
    },
  });

  const itemLibrary = libs?.libraries.find((l) => l.id === item?.library_id);
  const back = useBack(
    item?.parent
      ? { to: `/items/${item.parent.id}`, label: item.parent.title }
      : { to: itemLibrary ? `/library/${itemLibrary.id}${from ? `?${from}` : ""}` : "/", label: itemLibrary?.name ?? "Back" },
  );

  if (isLoading) return <Spinner />;
  if (error || !item) return <ErrorNote error={error ?? "Not found"} />;
  const library = libs?.libraries.find((l) => l.id === item.library_id);
  const expected = library?.server_libraries.filter((s) => s.enabled).map((s) => s.server_id) ?? [];

  const ids = [
    item.tmdb_id && { label: `TMDb ${item.tmdb_id}`, href: `https://www.themoviedb.org/${item.kind === "show" ? "tv" : "movie"}/${item.tmdb_id}` },
    item.tvdb_id && { label: `TVDB ${item.tvdb_id}`, href: `https://thetvdb.com/dereferrer/${item.kind === "show" ? "series" : "movie"}/${item.tvdb_id}` },
    item.imdb_id && { label: `IMDb ${item.imdb_id}`, href: `https://www.imdb.com/title/${item.imdb_id}` },
  ].filter(Boolean) as { label: string; href: string }[];

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <a href={back.href} onClick={back.go} className="inline-flex items-center gap-1 text-sm text-muted hover:text-text">
          <ArrowLeft className="size-4" /> {back.label}
        </a>
        {(item.kind === "movie" || item.kind === "show") && <PrevNext kind="items" id={item.id} from={from} />}
        {item.kind === "season" && item.parent && <SeasonNav showId={item.parent.id} seasonId={item.id} />}
      </div>
      <PageHeader
        title={
          <>
            {item.title} {item.year && item.kind !== "season" && <span className="font-normal text-muted">({item.year})</span>}
          </>
        }
        subtitle={
          <span className="flex flex-wrap items-center gap-3">
            <PresenceChips servers={servers} presentIds={item.servers.map((s) => s.server_id)} expectedIds={expected} />
            {ids.map((i) => (
              <a key={i.label} href={i.href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-text">
                {i.label} <ExternalLink className="size-3" />
              </a>
            ))}
            {!ids.length && item.kind !== "season" && <span className="text-warn">No TMDb/TVDB/IMDb ids: matched by title and year</span>}
          </span>
        }
        actions={
          <Button
            variant="ghost"
            icon={<RefreshCw className="size-4" />}
            loading={reload.running}
            onClick={() => reload.run(undefined)}
            title={`Re-read this ${item.kind === "movie" ? "movie" : "show, its seasons and episodes"} from every server, without a full import`}
          >
            Refresh
          </Button>
        }
      />
      <ErrorNote error={reload.error} />
      {reload.job && !reload.running && (
        <div
          className={
            reload.job.status === "failed"
              ? "mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger"
              : "mb-4 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm"
          }
        >
          {reload.job.status === "failed"
            ? reload.job.error
            : reload.job.status === "cancelled"
            ? "Refresh cancelled."
            : ((reload.job.result?.changes as string[] | undefined) ?? []).join(" · ") || "Refreshed."}
        </div>
      )}

      <PendingPush mediaItemId={item.id} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card className="p-5">
          <h2 className="mb-1 font-semibold">Posters on each server</h2>
          <p className="mb-4 text-sm text-muted">
            Pick the clean base artwork. It's queued under Changes until you push it to every server.
          </p>
          <PosterChooser
            title={item.title}
            presence={item.servers}
            artwork={item.artwork}
            servers={servers}
            posterSource={item.poster_source}
            onChoose={(id) => choose.mutate(id)}
            busy={choose.isPending}
            owner={{ kind: "items", id: item.id, sets: item.kind === "movie" || item.kind === "show" }}
          />
          <ErrorNote error={choose.error} />
        </Card>

        <div className="space-y-6">
          {item.issues.length > 0 && (
            <Card className="p-5">
              <h2 className="mb-3 font-semibold">Differences</h2>
              <ul className="space-y-2 text-sm">
                {item.issues.map((i) => (
                  <li key={i.id} className={i.status === "ignored" ? "text-muted line-through" : ""}>
                    {describeIssue(i, servers)}
                  </li>
                ))}
              </ul>
            </Card>
          )}
          {item.kind !== "season" && (
          <Card className="p-5">
            <h2 className="mb-3 font-semibold">Collections</h2>
            {item.collections.length ? (
              <ul className="space-y-1 text-sm">
                {item.collections.map((c) => (
                  <li key={c.id}>
                    <Link to={`/collections/${c.id}`} className="text-accent hover:underline">
                      {c.name}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted">Not in any collection.</p>
            )}
          </Card>
          )}
        </div>
      </div>

      {item.kind === "show" && item.seasons.length > 0 && (
        <Card className="mt-6 p-5">
          <h2 className="mb-1 font-semibold">Seasons</h2>
          <p className="mb-4 text-sm text-muted">Open a season to choose, upload or find its poster.</p>
          <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 lg:grid-cols-6 2xl:grid-cols-8">
            {item.seasons.map((s) => {
              const max = Math.max(0, ...Object.values(s.episodes));
              const short = expected.filter((sid) => (s.episodes[String(sid)] ?? 0) < max);
              return (
                <Link key={s.id} to={`/items/${s.id}`} className="group">
                  <div className="relative">
                    <Poster presence={s.poster} alt={s.title} width={300} className="ring-accent/0 transition group-hover:ring-2 group-hover:ring-accent" />
                    {s.poster_source && (
                      <span className="absolute right-1.5 top-1.5 rounded bg-accent px-1.5 py-0.5 text-[10px] font-bold text-black">Own poster</span>
                    )}
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2 text-sm">
                    <span className="truncate font-medium">{s.season_number === 0 ? "Specials" : s.title}</span>
                    <PresenceChips servers={servers} presentIds={s.servers.map((p) => p.server_id)} expectedIds={expected} />
                  </div>
                  <div
                    className={cx("text-xs", short.length ? "text-warn" : "text-muted")}
                    title={expected.map((sid) => `${servers.get(sid)?.name}: ${s.episodes[String(sid)] ?? 0}`).join(", ")}
                  >
                    {max} episode{max === 1 ? "" : "s"}
                    {short.length > 0 && ` · fewer on ${short.map((sid) => servers.get(sid)?.name).join(", ")}`}
                  </div>
                </Link>
              );
            })}
          </div>
        </Card>
      )}
    </>
  );
}

/** Previous / next season of the same show. */
function SeasonNav({ showId, seasonId }: { showId: number; seasonId: number }) {
  const { data: show } = useQuery({ queryKey: ["item", String(showId)], queryFn: () => api.get<ItemDetail>(`/api/items/${showId}`) });
  const seasons = show?.seasons ?? [];
  const i = seasons.findIndex((s) => s.id === seasonId);
  if (i < 0) return null;
  const button = "inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-sm";
  const link = (season: (typeof seasons)[number] | undefined, label: ReactNode) =>
    season ? (
      <Link to={`/items/${season.id}`} replace className={`${button} hover:bg-panel-2`}>
        {label}
      </Link>
    ) : (
      <span className={`${button} opacity-40`}>{label}</span>
    );
  return (
    <div className="flex items-center gap-2">
      {link(
        seasons[i - 1],
        <>
          <ChevronLeft className="size-4" /> Previous season
        </>,
      )}
      <span className="text-xs text-muted">
        {i + 1} of {seasons.length}
      </span>
      {link(
        seasons[i + 1],
        <>
          Next season <ChevronRight className="size-4" />
        </>,
      )}
    </div>
  );
}
