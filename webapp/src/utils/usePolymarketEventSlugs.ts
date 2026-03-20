import { useEffect, useState } from "react";

/**
 * Fetches active Polymarket events (sorted by 24h volume) and returns a map
 * from conditionId → market slug (used in https://polymarket.com/event/{slug}).
 */
export function usePolymarketEventSlugs() {
  const [slugMap, setSlugMap] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    fetch(
      "https://gamma-api.polymarket.com/events?limit=500&active=true&closed=false&order=volume24hr&ascending=false"
    )
      .then((r) => r.json())
      .then((json) => {
        if (cancelled) return;
        const events: any[] = Array.isArray(json) ? json : (json.events ?? []);
        const map: Record<string, string> = {};
        for (const ev of events) {
          for (const m of ev.markets ?? []) {
            // Use the event-level slug — Polymarket's canonical URL is /event/{event-slug}
            const slug: string = ev.slug || m.slug;
            if (slug && m.conditionId) map[m.conditionId] = slug;
          }
        }
        setSlugMap(map);
      })
      .catch(() => {/* silently ignore — links will fall back to plain text */});
    return () => { cancelled = true; };
  }, []);

  return slugMap;
}
