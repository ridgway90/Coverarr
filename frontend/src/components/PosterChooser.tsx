import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Layers, Trash2 } from "lucide-react";
import { api, type Artwork, type Presence, type Server } from "../lib/api";
import ArtworkTools, { useArtworkRefresh, type ArtworkOwner } from "./ArtworkTools";
import { Button, ErrorNote, Poster, ServerChip, cx } from "./ui";

const ARTWORK_LABELS: Record<string, string> = {
  kometa: "Kometa original",
  upload: "Uploaded",
  tmdb: "TMDb",
  set: "Poster set",
  original: "Original",
};

/** Posters on each server plus artwork Coverarr holds, with a button to make one the base. */
export default function PosterChooser({
  title,
  presence,
  artwork = [],
  servers,
  posterSource,
  onChoose,
  busy,
  owner,
}: {
  title: string;
  presence: Presence[];
  artwork?: Artwork[];
  servers: Map<number, Server>;
  posterSource: string | null;
  /** "server:<id>", "artwork:<id>", or null to clear. */
  onChoose: (source: string | null) => void;
  busy?: boolean;
  /** Shows "Upload poster" / "Find on TMDb" and lets you delete Coverarr's artwork. */
  owner?: ArtworkOwner;
}) {
  const refresh = useArtworkRefresh();
  const remove = useMutation({
    mutationFn: (artworkId: number) => api.del(`/api/artwork/${artworkId}`),
    onSuccess: refresh,
  });
  const options: {
    key: string;
    artworkId: number | null;
    presence: Presence | Artwork;
    label: string;
    chip: Server | null;
    note: string | null;
  }[] = [
    ...artwork.map((a) => ({
      key: `artwork:${a.artwork_id}`,
      artworkId: a.artwork_id,
      presence: a,
      label: ARTWORK_LABELS[a.source] ?? "Artwork",
      chip: null,
      note: a.width && a.height ? `${a.width}×${a.height} · clean, no overlays` : "Clean, no overlays",
    })),
    ...presence.map((p) => {
      const server = servers.get(p.server_id) ?? null;
      return {
        key: `server:${p.server_id}`,
        artworkId: null,
        presence: p,
        label: server?.name ?? "Server",
        chip: server,
        note: p.kometa_overlay ? "Has Kometa overlay" : null,
      };
    }),
  ];
  return (
    <div>
      {owner && <ArtworkTools owner={owner} />}
      <ErrorNote error={remove.error} />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        {options.map((o) => {
          const isChosen = posterSource === o.key;
          return (
            <div key={o.key} className={cx("rounded-lg border p-2", isChosen ? "border-accent bg-accent/5" : "border-line")}>
              <Poster presence={o.presence} alt={`${title}: ${o.label}`} width={400} />
              <div className="mt-2 flex items-center gap-2 text-sm">
                {o.chip ? <ServerChip server={o.chip} /> : <span className="rounded bg-accent/20 px-1 text-[10px] font-bold uppercase text-accent">CV</span>}
                <span className="flex-1 truncate font-medium">{o.label}</span>
                {owner && o.artworkId !== null && (
                  <button
                    className="text-muted hover:text-danger disabled:opacity-50"
                    title="Delete this artwork from Coverarr"
                    aria-label="Delete artwork"
                    disabled={remove.isPending}
                    onClick={() => confirm(`Delete this ${o.label.toLowerCase()} poster from Coverarr?`) && remove.mutate(o.artworkId!)}
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                )}
              </div>
              {o.note && (
                <div className={cx("mt-1 flex items-center gap-1 text-xs", o.presence.kometa_overlay ? "text-warn" : "text-muted")}>
                  {o.presence.kometa_overlay && <Layers className="size-3.5" />} {o.note}
                </div>
              )}
              <div className="mt-2">
                {isChosen ? (
                  <div className="flex items-center gap-1 text-sm text-accent">
                    <CheckCircle2 className="size-4" /> Base poster
                  </div>
                ) : (
                  <Button size="sm" className="w-full" disabled={!o.presence.has_poster || busy} onClick={() => onChoose(o.key)}>
                    Use as base
                  </Button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {posterSource && (
        <button className="mt-3 text-xs text-muted underline hover:text-text" onClick={() => onChoose(null)} disabled={busy}>
          Clear base poster choice
        </button>
      )}
    </div>
  );
}
