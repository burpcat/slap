import type { CSSProperties } from 'react';
import { useTheme } from '../../theme/useTheme';
import type { CampaignColor } from '../../api/types';
import styles from './Chip.module.css';

const COLOR_CLASS: Record<string, string> = {
  good: styles.good,
  serious: styles.serious,
  critical: styles.critical,
  warning: styles.warning,
};

/** Renders a {color,label} status chip exactly as _status_chip() (dashboard.py)
 * computes it -- color null means "label only, no fill" (neutral). */
export function StatusChip({ color, label }: { color: string | null; label: string }) {
  const cls = color ? COLOR_CLASS[color] ?? styles.neutral : styles.neutral;
  return <span className={`${styles.chip} ${cls}`}>{label}</span>;
}

/** A small color dot in a campaign's identity hue (color.py's campaign_colors,
 * {light,dark}) -- the ONLY place a per-campaign hex is used, and only via an
 * inline CSS custom property, never a class/hex literal (see build brief). */
export function CampaignDot({ color }: { color: CampaignColor }) {
  const { effective } = useTheme();
  const style = { background: effective === 'dark' ? color.dark : color.light } as CSSProperties;
  return <span className={styles.dot} style={style} aria-hidden="true" />;
}

export function CampaignChip({ name, color }: { name: string; color: CampaignColor }) {
  const { effective } = useTheme();
  const style = {
    background: 'var(--hairline)',
    color: 'var(--text-primary)',
    borderLeft: `3px solid ${effective === 'dark' ? color.dark : color.light}`,
  } as CSSProperties;
  return (
    <span className={styles.chip} style={style}>
      {name}
    </span>
  );
}
