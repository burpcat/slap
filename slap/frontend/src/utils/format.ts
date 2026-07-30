// Small display-only formatting helpers shared across pages. Nothing here
// touches the API or business logic — purely turning already-resolved values
// (owner-local ISO strings, domains, counts) into human-readable strings.

/** Strip the public-suffix tail from a domain for the front-page company word
 * cloud: "salesforce.com" -> "salesforce", "bluefishai.com" -> "bluefishai".
 * Deliberately simple — drops only the final dot-segment (the common .com/.io
 * case). A two-part suffix like ".co.uk" keeps "co", which is acceptable for a
 * glanceable word cloud; the real, dedup-relevant key stays the full domain
 * server-side. */
export function stripTld(domain: string): string {
  const dot = domain.lastIndexOf('.');
  return dot > 0 ? domain.slice(0, dot) : domain;
}

/** Whole days between an owner-local ISO date/timestamp and now (>= 0 for past,
 * negative for future). Used for "N days ago" style status text. */
export function daysSince(iso: string | null): number | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  return Math.floor((Date.now() - then) / 86_400_000);
}

/** A short "Xd Yh Zm" (or "due") label for a countdown to `targetMs`. Returns
 * `done: true` once the target has passed so callers can flip styling/state. */
export function countdownTo(targetMs: number): { done: boolean; label: string } {
  const remaining = targetMs - Date.now();
  if (remaining <= 0) return { done: true, label: 'due now' };
  const days = Math.floor(remaining / 86_400_000);
  const hours = Math.floor((remaining % 86_400_000) / 3_600_000);
  const mins = Math.floor((remaining % 3_600_000) / 60_000);
  if (days > 0) return { done: false, label: `${days}d ${hours}h` };
  if (hours > 0) return { done: false, label: `${hours}h ${mins}m` };
  return { done: false, label: `${mins}m` };
}

/** Owner-local short date (the server already localized the underlying value;
 * this is just presentation). Returns "—" for null/empty. */
export function shortDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString();
}
