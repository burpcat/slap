import { useMemo, useState } from 'react';
import type { BounceRow } from '../api/types';
import { StatusChip } from './primitives/Chip';
import { Button } from './primitives/Button';
import { shortDate } from '../utils/format';
import styles from './BounceItem.module.css';

// The GMass bounce `reason` is a raw DSN — sometimes one long run with "**"
// acting as line breaks, sometimes plain space/newline-separated with no "**"
// at all. We want two things from it: a SHORT "broader details" summary for the
// row, and the full dump kept out of sight behind View log (req 8, Image #10).
//
// The summary is deliberately terse — SMTP status code + a trimmed diagnostic
// sentence — never the whole dump (the earlier version fell back to the entire
// blob when there was no "**", which is exactly what leaked into the row).
function shortSummary(raw: string): string {
  const status = raw.match(/status:\s*([0-9]\.[0-9]\.[0-9])/i)?.[1];
  // The human-readable bit follows "Diagnostic-Code: smtp; <code> <text>".
  // Cut it off at the first URL, "[", "Last-Attempt", or a hard length cap so a
  // verbose provider message can't sprawl.
  let diag = raw.match(/diagnostic-code:\s*smtp;\s*(.+)/i)?.[1]?.trim() ?? '';
  diag = diag.split(/https?:\/\/|\s\[|Last-Attempt-Date/i)[0].trim();
  if (diag.length > 120) diag = diag.slice(0, 117).trimEnd() + '…';
  // Category is already shown by the chip beside this line — no need to repeat
  // it. Fall back to a generic note only if the DSN had neither field.
  const parts = [status, diag].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'Delivery failed — see log for details';
}

// The full dump, broken into readable lines for the View-log view. Prefer the
// "**" delimiter when present, else split on newlines, else keep the raw blob
// as a single (pre-wrapped) line — never lose content here.
function fullLines(raw: string): string[] {
  const parts = (raw.includes('**') ? raw.split('**') : raw.split(/\r?\n/))
    .map((l) => l.trim())
    .filter(Boolean);
  return parts.length ? parts : [raw];
}

export function BounceItem({ bounce }: { bounce: BounceRow }) {
  const [open, setOpen] = useState(false);
  const isBlock = bounce.category === 'block';
  const summary = useMemo(() => shortSummary(bounce.reason || ''), [bounce.reason]);
  const lines = useMemo(() => fullLines(bounce.reason || ''), [bounce.reason]);

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
