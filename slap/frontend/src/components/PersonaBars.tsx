import { useMemo } from 'react';
import styles from './PersonaBars.module.css';

/** Reply-rate-by-persona as a tidy horizontal bar list (req 5, 7 — replaces
 * the uneven square-tile grid). Each row is a fixed-width label + a bar + the
 * value, so rows line up regardless of persona-name length. Bars scale to the
 * largest rate present (reply rates are all low single-digit %, so scaling to
 * 100 would leave every bar a nub); the printed % is always the true value. */
export function PersonaBars({ rates }: { rates: Record<string, number> }) {
  const rows = useMemo(() => {
    const entries = Object.entries(rates);
    const max = Math.max(1, ...entries.map(([, v]) => v));
    return entries
      .sort((a, b) => b[1] - a[1])
      .map(([persona, pct]) => ({ persona, pct, width: (pct / max) * 100 }));
  }, [rates]);

  if (rows.length === 0) return <p className={styles.empty}>No engagement data yet.</p>;

  return (
    <div className={styles.list}>
      {rows.map((r) => (
        <div key={r.persona} className={styles.row}>
          <span className={styles.label}>{r.persona}</span>
          <div className={styles.track}>
            <div className={styles.fill} style={{ width: `${Math.max(r.width, r.pct > 0 ? 2 : 0)}%` }} />
          </div>
          <span className={styles.value}>{r.pct}%</span>
        </div>
      ))}
    </div>
  );
}
