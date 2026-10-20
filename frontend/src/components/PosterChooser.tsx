import { CheckCircle2, Layers } from "lucide-react";
import type { Presence, Server } from "../lib/api";
import { Button, Poster, ServerChip, cx } from "./ui";

/** Side-by-side posters from each server, with a button to make one the clean base. */
export default function PosterChooser({
  title,
  presence,
  servers,
  posterSource,
  onChoose,
  busy,
}: {
  title: string;
  presence: Presence[];
  servers: Map<number, Server>;
  posterSource: string | null;
  onChoose: (serverId: number | null) => void;
  busy?: boolean;
}) {
  const chosen = posterSource?.startsWith("server:") ? Number(posterSource.split(":")[1]) : null;
  return (
    <div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        {presence.map((p) => {
          const server = servers.get(p.server_id);
          const isChosen = chosen === p.server_id;
          return (
            <div key={p.server_id} className={cx("rounded-lg border p-2", isChosen ? "border-accent bg-accent/5" : "border-line")}>
              <Poster presence={p} alt={`${title} on ${server?.name ?? "server"}`} width={400} />
              <div className="mt-2 flex items-center gap-2 text-sm">
                {server && <ServerChip server={server} />}
                <span className="truncate font-medium">{server?.name}</span>
              </div>
              {p.kometa_overlay && (
                <div className="mt-1 flex items-center gap-1 text-xs text-warn" title="Kometa has drawn overlays on this poster">
                  <Layers className="size-3.5" /> Has Kometa overlay
                </div>
              )}
              <div className="mt-2">
                {isChosen ? (
                  <div className="flex items-center gap-1 text-sm text-accent">
                    <CheckCircle2 className="size-4" /> Base poster
                  </div>
                ) : (
                  <Button size="sm" className="w-full" disabled={!p.has_poster || busy} onClick={() => onChoose(p.server_id)}>
                    Use as base
                  </Button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {chosen !== null && (
        <button className="mt-3 text-xs text-muted hover:text-text underline" onClick={() => onChoose(null)} disabled={busy}>
          Clear base poster choice
        </button>
      )}
    </div>
  );
}
