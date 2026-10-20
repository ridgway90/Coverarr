import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, FileVideo, Sparkles, Wand2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Issue, type Server } from "../lib/api";
import { Button, ErrorNote, Field, Modal, Poster, ServerChip, cx, inputClass } from "./ui";

interface Candidate {
  media_item_id: number;
  title: string;
  year: number | null;
  tmdb_id: string | null;
  tvdb_id: string | null;
  imdb_id: string | null;
  servers: number[];
  server_items: { server_id: number; server_item_id: number; has_poster: boolean; poster_version: string }[];
  score: number;
  reasons: string[];
}

interface Target {
  tmdb_id?: string | null;
  tvdb_id?: string | null;
  imdb_id?: string | null;
  title?: string | null;
  year?: number | null;
}

const idLabel = (t: Target) =>
  [t.tmdb_id && `TMDb ${t.tmdb_id}`, t.tvdb_id && `TVDB ${t.tvdb_id}`, t.imdb_id && `IMDb ${t.imdb_id}`].filter(Boolean).join(" · ");

const describe = (t: Target) => (t.title ? `${t.title}${t.year ? ` (${t.year})` : ""}` : idLabel(t));

export default function MismatchRow({ issue, servers }: { issue: Issue; servers: Map<number, Server> }) {
  const d = issue.details as {
    kind: "movie" | "show";
    path: string;
    hints: Target;
    candidates: Candidate[];
    suggested: number | null;
    confidence: "high" | "medium" | "low" | "none";
  };
  const [target, setTarget] = useState<Target | null>(null);
  const [manual, setManual] = useState(false);
  const fileName = d.path.replace(/\\/g, "/").split("/").slice(-2).join("/");

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{issue.title}</span>
        <span className="text-sm text-muted">is matched to different {d.kind === "movie" ? "films" : "shows"} on different servers</span>
      </div>
      <div className="mt-1 flex items-center gap-1.5 break-all font-mono text-xs text-muted" title={d.path}>
        <FileVideo className="size-3.5 shrink-0" /> {fileName}
      </div>

      <div className="mt-3 flex flex-wrap gap-3">
        {d.candidates.map((c) => {
          const suggested = c.media_item_id === d.suggested;
          const first = c.server_items.find((s) => s.has_poster) ?? c.server_items[0];
          return (
            <div key={c.media_item_id} className={cx("w-60 rounded-lg border p-3", suggested ? "border-accent bg-accent/5" : "border-line")}>
              <div className="flex gap-3">
                <Poster presence={{ ...first, kometa_overlay: false }} alt={c.title} width={160} className="w-20 shrink-0" />
                <div className="min-w-0 text-sm">
                  <Link to={`/items/${c.media_item_id}`} className="font-medium hover:text-accent">
                    {c.title} {c.year && <span className="text-muted">({c.year})</span>}
                  </Link>
                  <div className="mt-0.5 text-xs text-muted">{idLabel(c)}</div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {c.servers.map((sid) => {
                      const s = servers.get(sid);
                      return s ? <ServerChip key={sid} server={s} title={`${s.name} says this`} /> : null;
                    })}
                  </div>
                </div>
              </div>
              {suggested && (
                <div className="mt-2 flex items-center gap-1 text-xs font-medium text-accent">
                  <Sparkles className="size-3.5" /> Best guess ({d.confidence} confidence)
                </div>
              )}
              {c.reasons.length > 0 && (
                <ul className="mt-1 space-y-0.5 text-xs text-muted">
                  {c.reasons.map((r) => (
                    <li key={r}>• {r}</li>
                  ))}
                </ul>
              )}
              <Button size="sm" className="mt-3 w-full" variant={suggested ? "primary" : "secondary"} onClick={() => setTarget(c)}>
                This is correct…
              </Button>
            </div>
          );
        })}
        <div className="flex w-48 flex-col items-center justify-center rounded-lg border border-dashed border-line p-3 text-center text-xs text-muted">
          None of these?
          <Button size="sm" className="mt-2" onClick={() => setManual(true)}>
            Enter an ID…
          </Button>
        </div>
      </div>

      {manual && (
        <ManualTarget
          kind={d.kind}
          onClose={() => setManual(false)}
          onPick={(t) => {
            setManual(false);
            setTarget(t);
          }}
        />
      )}
      {target && <ConfirmFix issue={issue} target={target} details={d} servers={servers} onClose={() => setTarget(null)} />}
    </div>
  );
}

function ManualTarget({ kind, onClose, onPick }: { kind: "movie" | "show"; onClose: () => void; onPick: (t: Target) => void }) {
  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [year, setYear] = useState("");
  const key = kind === "movie" ? "tmdb_id" : "tvdb_id";
  const valid = /^\d+$/.test(id.trim());
  return (
    <Modal
      open
      onClose={onClose}
      title="Match to a different title"
      footer={
        <Button variant="primary" disabled={!valid} onClick={() => onPick({ [key]: id.trim(), title: title.trim() || null, year: year ? Number(year) : null })}>
          Continue
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label={kind === "movie" ? "TMDb id" : "TVDB id"} hint={kind === "movie" ? "The number in themoviedb.org/movie/<id>" : "The series id on thetvdb.com"}>
          <input className={inputClass} value={id} onChange={(e) => setId(e.target.value)} inputMode="numeric" autoFocus />
        </Field>
        <div className="grid grid-cols-[1fr_7rem] gap-3">
          <Field label="Title (optional)" hint="Shown in the log, and used if a server can't search by id">
            <input className={inputClass} value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Year">
            <input className={inputClass} value={year} onChange={(e) => setYear(e.target.value.replace(/\D/g, "").slice(0, 4))} inputMode="numeric" />
          </Field>
        </div>
      </div>
    </Modal>
  );
}

function ConfirmFix({
  issue,
  target,
  details,
  servers,
  onClose,
}: {
  issue: Issue;
  target: Target;
  details: { kind: "movie" | "show"; hints: Target; candidates: Candidate[] };
  servers: Map<number, Server>;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const fix = useMutation({
    mutationFn: () =>
      api.post<{ job_id: number }>(`/api/review/issues/${issue.id}/fix-match`, {
        target: { tmdb_id: target.tmdb_id, tvdb_id: target.tvdb_id, imdb_id: target.imdb_id, title: target.title, year: target.year },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });

  const matches = (c: Candidate) =>
    (target.tmdb_id && c.tmdb_id === target.tmdb_id) ||
    (target.tvdb_id && c.tvdb_id === target.tvdb_id) ||
    (target.imdb_id && c.imdb_id === target.imdb_id);
  const changes = details.candidates.filter((c) => !matches(c));
  const unchanged = details.candidates.filter((c) => matches(c)).flatMap((c) => c.servers);
  const hintIds = details.hints;
  const fileDisagrees =
    (hintIds.tmdb_id && target.tmdb_id && hintIds.tmdb_id !== target.tmdb_id) ||
    (hintIds.tvdb_id && target.tvdb_id && hintIds.tvdb_id !== target.tvdb_id) ||
    (hintIds.imdb_id && target.imdb_id && hintIds.imdb_id !== target.imdb_id);
  const arr = details.kind === "movie" ? "Radarr" : "Sonarr";

  return (
    <Modal
      open
      onClose={onClose}
      title="Confirm re-match"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon={<Wand2 className="size-4" />} loading={fix.isPending} onClick={() => fix.mutate()}>
            Re-match
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <p>
          Match this file to <span className="font-semibold">{describe(target)}</span>
          {target.title && <span className="text-muted"> ({idLabel(target)})</span>} on every server.
        </p>
        <ul className="space-y-1">
          {changes.flatMap((c) =>
            c.servers.map((sid) => (
              <li key={`${c.media_item_id}-${sid}`} className="flex items-center gap-2">
                {servers.get(sid) && <ServerChip server={servers.get(sid)!} />}
                <span>
                  {servers.get(sid)?.name}: <span className="text-muted line-through">{describe(c)}</span> → {describe(target)}
                </span>
              </li>
            )),
          )}
          {unchanged.length > 0 && (
            <li className="text-muted">Already correct on {unchanged.map((sid) => servers.get(sid)?.name).join(", ")}.</li>
          )}
        </ul>
        <p className="text-xs text-muted">
          Uses each server's own "fix match", which also fetches that title's metadata and artwork. Check the result in
          Activity.
        </p>
        {fileDisagrees && (
          <div className="flex gap-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
            <span>
              The file name says <span className="font-mono">{idLabel(hintIds)}</span>, which is a different title. If the
              file really is {describe(target)}, fix it in {arr} too. Otherwise {arr} may rename, upgrade or replace it as
              the wrong title.
            </span>
          </div>
        )}
        <ErrorNote error={fix.error} />
      </div>
    </Modal>
  );
}
