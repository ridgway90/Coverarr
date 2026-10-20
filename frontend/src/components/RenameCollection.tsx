import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "../lib/api";
import { Button, ErrorNote, Field, Modal, inputClass } from "./ui";

/** Rename a collection on every server it exists on. */
export default function RenameCollection({
  collectionId,
  currentName,
  onClose,
}: {
  collectionId: number;
  currentName: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(currentName);
  const rename = useMutation({
    mutationFn: () => api.post<{ job_id: number }>(`/api/collections/${collectionId}/rename`, { name: name.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });
  const unchanged = name.trim() === currentName || !name.trim();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!unchanged) rename.mutate();
  };
  return (
    <Modal
      open
      onClose={onClose}
      title="Rename collection"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={rename.isPending} disabled={unchanged} onClick={() => rename.mutate()}>
            Rename everywhere
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <Field label="New name" hint="Renamed on every server this collection is on. The name is locked so the server doesn't change it back.">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={300} />
        </Field>
        <ErrorNote error={rename.error} />
      </form>
    </Modal>
  );
}
