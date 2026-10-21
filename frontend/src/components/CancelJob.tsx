import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Square } from "lucide-react";
import { api, type Job } from "../lib/api";
import { Button } from "./ui";

/** Stop a queued or running job. Work it already finished (e.g. posters pushed) is kept. */
export default function CancelJob({ job, className }: { job: Pick<Job, "id" | "status"> | undefined; className?: string }) {
  const qc = useQueryClient();
  const cancel = useMutation({
    mutationFn: (id: number) => api.post(`/api/jobs/${id}/cancel`),
    onSettled: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  if (!job || (job.status !== "queued" && job.status !== "running")) return null;
  return (
    <Button
      size="sm"
      variant="danger"
      className={className}
      icon={<Square className="size-3.5" />}
      loading={cancel.isPending}
      onClick={(e) => {
        e.stopPropagation();
        if (job.status === "queued" || confirm("Cancel this task? Anything it has already done is kept.")) cancel.mutate(job.id);
      }}
    >
      Cancel
    </Button>
  );
}
