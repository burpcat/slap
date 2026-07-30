import { useMemo, useState } from 'react';
import type { BounceRow } from '../api/types';
import { StatusChip } from './primitives/Chip';
import { Button } from './primitives/Button';
import { shortDate } from '../utils/format';
import styles from './BounceItem.module.css';

// The GMass bounce `reason` arrives as one long run with "**" acting as line
// breaks (an artifact of how the DSN is captured). Split it back into lines,
// and lift the human-meaningful SMTP diagnostic to the top as the summary so
// the row is readable without opening the full log (req 8).
function parseReason(raw: string): { summary: string; lines: string[] } {
  const lines = raw
    .split('**')
    .map((l) => l.trim())
    .filter(Boolean);
  // Prefer the line carrying the actual SMTP explanation (a 5xx/4xx code),
  // else the Diagnostic-Code line, else the first line.
  const diag =
    lines.find((l) => /\b[45]\d\d[\s-]/.test(l)) ??
    lines.find((l) => /^diagnostic-code/i.test(l)) ??
    lines[0] ??
    raw;
  return { summary: diag, lines };
}

export function BounceItem({ bounce }: { bounce: BounceRow }) {
  const [open, setOpen] = useState(false);
  const { summary, lines } = useMemo(() => parseReason(bounce.reason || ''), [bounce.reason]);
  const isBlock = bounce.category === 'block';

  return (
    <div className={styles.item}>
      <div className={styles.head}>
        <div className={styles.who}>
          <StatusChip color="critical" label={isBlock ? 'Blocked' : 'Bounced'} />
          <div className={styles.whoText}>
            <span className={styles.recipient}>{bounce.recipient}</span>
            <span className={styles.meta}>
              {bounce.campaign} · {shortDate(bounce.last_event_at)}
            </span>
          </div>
        </div>
        <Button small onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide log' : 'View log'}
        </Button>
      </div>

      <p className={styles.summary}>{summary}</p>

      {open && (
        <pre className={styles.log}>
          {lines.map((l, i) => (
            <span key={i} className={styles.logLine}>
              {l}
            </span>
          ))}
        </pre>
      )}
    </div>
  );
}
