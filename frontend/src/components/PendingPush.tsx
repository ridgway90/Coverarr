import { useQuery } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { api, qs, type ChangesPage } from "../lib/api";
import { useJobAction, useServerMap } from "../lib/hooks";
import { Button, ErrorNote } from "./ui";

/** "Your chosen poster isn't on X yet" with a push button, for a title or collection page. */
export default function PendingPush({ mediaItemId, collectionId }: { mediaItemId?: number; collectionId?: number }) {
  const servers = useServerMap();
  const { data } = useQuery({
    queryKey: ["changes", "for", mediaItemId, collectionId],
    queryFn: () => api.get<ChangesPage>(`/api/changes${qs({ media_item_id: mediaItemId, collection_id: collectionId })}`),
  });
  const push = useJobAction(
    () =>
      api.post<{ job_id: number }>(
        "/api/changes/push",
        mediaItemId !== undefined ? { media_item_ids: [mediaItemId] } : { collection_ids: [collectionId] },
      ),
    ["changes", "item", "items", "collection", "collections"],
  );
  const change = data?.items[0];
  const failed = push.job?.status === "failed" ? push.job.error : null;
  if (!change) return null;
  const names = change.servers.map((s) => servers.get(s.server_id)?.name ?? "a server");
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-accent/30 bg-accent/10 px-4 py-3 text-sm">
      <div>
        {change.problem ? (
          <span className="text-danger">{change.problem}</span>
        ) : (
          <>
            The chosen poster isn't on <strong>{names.join(", ")}</strong> yet.
            <span className="text-muted"> ({[...new Set(change.servers.map((s) => s.reason))].join(", ")})</span>
          </>
        )}
        <ErrorNote error={push.error ?? failed} />
      </div>
      {!change.problem && (
        <Button
          variant="primary"
          size="sm"
          icon={<Upload className="size-3.5" />}
          loading={push.running}
          onClick={() => push.run(undefined)}
        >
          Push now
        </Button>
      )}
    </div>
  );
}
