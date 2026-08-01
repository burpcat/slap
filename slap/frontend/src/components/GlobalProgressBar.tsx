import { useEffect, useRef, useState } from 'react';
import { useIsFetching } from '@tanstack/react-query';
import styles from './GlobalProgressBar.module.css';

// Once the bar turns on it stays visible for at least this long, even if the
// triggering fetch resolves near-instantly (the common case with a warm cache).
// Without a floor, quick fetches flash the bar for a sub-frame no one can see.
const MIN_VISIBLE_MS = 500;

/** A slim, indeterminate loading bar pinned to the very top of the shell that
 * animates whenever ANY React Query fetch is in flight (useIsFetching() > 0) —
 * i.e. the whole "refresh process": initial loads, route switches, the 60s
 * sync-status/nav polls, mutation-triggered invalidations, and the manual
 * "Refresh now" GMass sync (whose onSuccess invalidations refetch home/
 * pipeline/engagement/sync-status). Kept as its own tiny component so only it
 * re-renders as the fetch count changes — the Layout around it never does.
 *
 * A MIN_VISIBLE_MS floor keeps the bar on long enough to actually see on fast
 * cached fetches: on the rising edge it shows immediately; on the falling edge
 * it stays until the floor elapses (a pending hide is cancelled if another
 * fetch starts first). position:fixed so it overlays the top edge instead of
 * shifting layout; always mounted and toggled via an `active` class + opacity
 * transition rather than mount/unmount. See GlobalProgressBar.module.css for
 * the animation and the reduced-motion fallback. */
export function GlobalProgressBar() {
  const fetching = useIsFetching() > 0;
  const [visible, setVisible] = useState(false);
  const shownAtRef = useRef(0);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (fetching) {
      // Rising edge: cancel any pending hide and show immediately, stamping
      // when we became visible so the falling edge can honor the floor.
      if (hideTimerRef.current !== null) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      if (!visible) {
        shownAtRef.current = Date.now();
        setVisible(true);
      }
      return;
    }
    // Falling edge: hide, but not before the min-visible floor has elapsed.
    if (visible && hideTimerRef.current === null) {
      const remaining = MIN_VISIBLE_MS - (Date.now() - shownAtRef.current);
      if (remaining <= 0) {
        setVisible(false);
      } else {
        hideTimerRef.current = setTimeout(() => {
          hideTimerRef.current = null;
          setVisible(false);
        }, remaining);
      }
    }
  }, [fetching, visible]);

  useEffect(() => () => {
    if (hideTimerRef.current !== null) clearTimeout(hideTimerRef.current);
  }, []);

  return (
    <div
      className={`${styles.bar} ${visible ? styles.active : ''}`}
      role="progressbar"
      aria-hidden={!visible}
      aria-label="Refreshing data"
    >
      <div className={styles.indicator} />
    </div>
  );
}
