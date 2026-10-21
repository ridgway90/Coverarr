import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Card, ErrorNote, PageHeader, Spinner } from "../components/ui";
import { api } from "../lib/api";

/** Where Trakt sends the browser after sign-in: finishes it with the code in the address. */
export default function TraktCallbackPage() {
  const qc = useQueryClient();
  const [state, setState] = useState<{ done?: string; error?: unknown }>({});
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return; // once, even in React's double-run dev mode
    started.current = true;
    api
      .post<{ username: string | null }>("/api/trakt/callback", { response: window.location.href })
      .then((s) => {
        qc.invalidateQueries({ queryKey: ["trakt"] });
        setState({ done: s.username ?? "" });
      })
      .catch((error) => setState({ error }));
  }, [qc]);

  return (
    <>
      <PageHeader title="Trakt" />
      <Card className="max-w-xl p-5 text-sm">
        {state.done !== undefined ? (
          <p>Signed in to Trakt{state.done ? ` as ${state.done}` : ""}. Private lists can be used in list collections now.</p>
        ) : state.error ? (
          <ErrorNote error={state.error} />
        ) : (
          <Spinner label="Finishing the Trakt sign-in…" />
        )}
        <Link to="/settings?tab=general" className="mt-3 inline-block text-accent hover:underline">
          Back to Settings
        </Link>
      </Card>
    </>
  );
}
