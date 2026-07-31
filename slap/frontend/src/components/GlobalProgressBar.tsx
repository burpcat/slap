import { useIsFetching } from '@tanstack/react-query';
import styles from './GlobalProgressBar.module.css';

/** A slim, indeterminate loading bar pinned to the very top of the shell that
 * animates whenever ANY React Query fetch is in flight (useIsFetching() > 0) —
 * i.e. the whole "refresh process": initial loads, route switches, the 60s
 * sync-status/nav polls, mutation-triggered invalidations, and the manual
 * "Refresh now" GMass sync (whose onSuccess invalidations refetch home/
 * pipeline/engagement/sync-status). Kept as its own tiny component so only it
 * re-renders as the fetch count changes — the Layout around it never does.
 *
 * position:fixed so it overlays the top edge instead of shifting layout when it
 * appears/disappears; always mounted and toggled via an `active` class + opacity
 * transition (a smooth fade-out that also debounces flicker on very fast fetches)
 * rather than mount/unmount. See GlobalProgressBar.module.css for the animation
 * and the reduced-motion fallback. */
export function GlobalProgressBar() {
  const active = useIsFetching() > 0;
  return (
    <div
      className={`${styles.bar} ${active ? styles.active : ''}`}
      role="progressbar"
      aria-hidden={!active}
      aria-label="Refreshing data"
    >
      <div className={styles.indicator} />
    </div>
  );
}
