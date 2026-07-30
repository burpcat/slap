import { useEffect, useState } from 'react';
import { countdownTo } from '../utils/format';
import styles from './Countdown.module.css';

/** A live, backwards-counting timer to `targetIso` (req 6 — warm-but-silent
 * "backwards timer"). Ticks once a minute (a follow-up window is measured in
 * days/hours; a per-second tick would be wasted renders). Flips to a muted
 * "due now" state once the target passes. */
export function Countdown({ targetIso }: { targetIso: string | null }) {
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  if (!targetIso) return null;
  const target = new Date(targetIso).getTime();
  if (Number.isNaN(target)) return null;
  const { done, label } = countdownTo(target);
  return <span className={`${styles.timer} ${done ? styles.due : ''}`}>{done ? 'ready to nudge' : `nudge in ${label}`}</span>;
}
