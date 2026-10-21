import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import { useLocation, useNavigate, useNavigationType, type Location } from "react-router-dom";
import type { Library } from "./api";

/**
 * Where each page was opened from, and how far each page was scrolled.
 *
 * Kept per history entry (``location.key``) in sessionStorage, so the Back link can return to
 * the exact list, collection or page you came from, at the same scroll position, even after a
 * reload. Previous/Next replace the history entry rather than adding one, so they inherit the
 * entry's origin and Back skips the titles stepped through.
 */
interface Origin {
  pathname: string;
  search: string;
}

const TRAIL_KEY = "coverarr:trail";
const SCROLL_KEY = "coverarr:scroll";
const MAX_ENTRIES = 300;

function load<T>(key: string): Record<string, T> {
  try {
    return JSON.parse(sessionStorage.getItem(key) ?? "{}");
  } catch {
    return {};
  }
}

function save<T>(key: string, data: Record<string, T>) {
  const keys = Object.keys(data);
  if (keys.length > MAX_ENTRIES) for (const k of keys.slice(0, keys.length - MAX_ENTRIES)) delete data[k];
  try {
    sessionStorage.setItem(key, JSON.stringify(data));
  } catch {
    /* storage full or disabled: back still works, just without the label */
  }
}

export function originOf(location: Location): Origin | null {
  return load<Origin>(TRAIL_KEY)[location.key] ?? null;
}

/** Records the trail and saves/restores scroll positions of ``scroller``. Use once, in the layout. */
export function useNavTrail(scroller: RefObject<HTMLElement | null>) {
  const location = useLocation();
  const navType = useNavigationType();
  const previous = useRef<Location | null>(null);

  useEffect(() => {
    const prev = previous.current;
    previous.current = location;
    if (!prev || prev.key === location.key) return;
    const trail = load<Origin>(TRAIL_KEY);
    if (navType === "PUSH") {
      trail[location.key] = { pathname: prev.pathname, search: prev.search };
    } else if (navType === "REPLACE" && trail[prev.key]) {
      trail[location.key] = trail[prev.key];
    } else {
      return;
    }
    save(TRAIL_KEY, trail);
  }, [location, navType]);

  // Remember the scroll offset of the current entry as it changes.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const positions = load<number>(SCROLL_KEY);
        positions[location.key] = el.scrollTop;
        save(SCROLL_KEY, positions);
      });
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      el.removeEventListener("scroll", onScroll);
    };
  }, [location.key, scroller]);

  // Going back/forward: restore the offset once the page is tall enough (lists load async).
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const target = navType === "POP" ? load<number>(SCROLL_KEY)[location.key] ?? 0 : 0;
    if (!target) {
      el.scrollTop = 0;
      return;
    }
    let tries = 0;
    let timer = 0;
    const attempt = () => {
      el.scrollTop = target;
      if (Math.abs(el.scrollTop - target) > 2 && tries++ < 40) timer = window.setTimeout(attempt, 50);
    };
    attempt();
    return () => window.clearTimeout(timer);
  }, [location.key, navType, scroller]);
}

/** A short name for a page, for "Back to …". */
export function describeOrigin(origin: Origin, qc: QueryClient): string {
  const params = new URLSearchParams(origin.search);
  const extras = (parts: (string | null | undefined)[]) => parts.filter(Boolean).join(" · ");
  const libraries = qc.getQueryData<{ libraries: Library[] }>(["libraries"])?.libraries ?? [];
  let m = origin.pathname.match(/^\/library\/(\d+)/);
  if (m) {
    const lib = libraries.find((l) => String(l.id) === m![1]);
    const filter = params.get("filter");
    return extras([lib?.name ?? "Library", params.get("letter"), params.get("q") && `"${params.get("q")}"`, filter && "filtered"]);
  }
  m = origin.pathname.match(/^\/collections\/(\d+)/);
  if (m) return qc.getQueryData<{ name: string }>(["collection", m[1]])?.name ?? "Collection";
  m = origin.pathname.match(/^\/items\/(\d+)/);
  if (m) return qc.getQueryData<{ title: string }>(["item", m[1]])?.title ?? "Title";
  if (origin.pathname.startsWith("/collections")) return extras(["Collections", params.get("letter")]);
  const named: Record<string, string> = {
    "/": "Dashboard",
    "/review": "Review",
    "/changes": "Changes",
    "/overlays": "Overlays",
    "/jobs": "Activity",
    "/settings": "Settings",
  };
  return named[origin.pathname] ?? "Back";
}

/** Back to wherever this page was opened from, else to ``fallback``. */
export function useBack(fallback: { to: string; label: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const origin = originOf(location);
  const canPop = origin !== null && ((window.history.state?.idx as number | undefined) ?? 0) > 0;
  return {
    label: origin ? describeOrigin(origin, qc) : fallback.label,
    href: origin ? origin.pathname + origin.search : fallback.to,
    go: (e?: { preventDefault: () => void }) => {
      e?.preventDefault();
      if (canPop) navigate(-1);
      else navigate(origin ? origin.pathname + origin.search : fallback.to);
    },
  };
}
