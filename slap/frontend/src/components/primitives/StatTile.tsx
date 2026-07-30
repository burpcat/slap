import type { ReactNode } from 'react';
import styles from './StatTile.module.css';

export function StatRow({ children }: { children: ReactNode }) {
  return <div className={styles.row}>{children}</div>;
}

export function StatTile({ value, suffix, label }: { value: ReactNode; suffix?: string; label: string }) {
  return (
    <div className={styles.tile}>
      <div className={styles.value}>
        {value}
        {suffix && <span className={styles.suffix}> {suffix}</span>}
      </div>
      <div className={styles.label}>{label}</div>
    </div>
  );
}

type Severity = 'good' | 'warning' | 'critical';

const FILL: Record<Severity, string> = {
  good: styles.good,
  warning: styles.warningFill,
  critical: styles.criticalFill,
};
const TRACK: Record<Severity, string> = {
  good: styles.goodTrack,
  warning: styles.warningTrack,
  critical: styles.criticalTrack,
};

/** A daily-cap-style gauge: a track tinted toward the same severity color as
 * the fill (dataviz skill's meter spec), pct clamped to [0,100]. */
export function Gauge({ pct, severity }: { pct: number; severity: Severity }) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div className={`${styles.gaugeTrack} ${TRACK[severity]}`}>
      <div className={`${styles.gaugeFill} ${FILL[severity]}`} style={{ width: `${clamped}%` }} />
    </div>
  );
}
