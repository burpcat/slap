import { useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  useLinkedinReplied,
  useReachouts,
  useResend,
  useStopOutreach,
  useTagReply,
} from '../api/hooks';
import { useTheme } from '../theme/useTheme';
import type { CampaignColor, ReachoutRow } from '../api/types';
import { StatusChip } from '../components/primitives/Chip';
import { RowMenu } from '../components/RowMenu';
import styles from './Reachouts.module.css';

type SortKey = 'recipient' | 'campaign' | 'persona' | 'status' | 'date_local';
type SortDir = 'asc' | 'desc';

function matchesSearch(row: ReachoutRow, needle: string): boolean {
  if (!needle) return true;
  const haystack = [row.recipient, row.company, row.name, row.domain, row.campaign, row.persona]
    .join(' ')
    .toLowerCase();
  return haystack.includes(needle);
}

function RowActions({ row, colors }: { row: ReachoutRow; colors: Record<string, CampaignColor> }) {
  const { effective } = useTheme();
  const tag = useTagReply(row.recipient);
  const stop = useStopOutreach(row.recipient);
  const resend = useResend(row.recipient);
  const linkedin = useLinkedinReplied(row.recipient);
  const color = colors[row.campaign];
  const tint = color ? (effective === 'dark' ? color.dark : color.light) : 'transparent';

  const rowStyle = { borderLeftColor: tint } as CSSProperties;

  return (
    <tr className={styles.row} style={rowStyle} data-status={row.status}>
      <td>
        <div className={styles.recipientCell}>
          <span>{row.recipient}</span>
          {(row.name || row.company) && (
            <span className={styles.recipientMeta}>
              {[row.name, row.company].filter(Boolean).join(' · ')}
            </span>
          )}
        </div>
      </td>
      <td>{row.campaign}</td>
      <td>{row.persona}</td>
      <td>
        <StatusChip color={row.chip.color} label={row.chip.label} />
      </td>
      <td>{row.date_local ?? '—'}</td>
      <td>
        <button
          className={`${styles.linkedin} ${row.linkedin_replied ? styles.linkedinActive : ''}`}
          title={row.linkedin_replied ? 'LinkedIn: replied (click to unmark)' : 'Mark LinkedIn replied'}
          onClick={() => linkedin.mutate({ replied: !row.linkedin_replied })}
          disabled={linkedin.isPending}
        >
          in
        </button>
      </td>
      <td>
        <RowMenu
          row={row}
          actions={{
            pending: tag.isPending || stop.isPending || resend.isPending,
            onMarkOoo: (resume_date) => tag.mutate({ tag: 'ooo', resume_date }),
            onStop: () => stop.mutate(),
            onResend: (corrected_email) => resend.mutate({ corrected_email }),
            onTagReal: () => tag.mutate({ tag: 'real' }),
            onTagNotInterested: () => tag.mutate({ tag: 'not_interested' }),
          }}
        />
      </td>
    </tr>
  );
}

export default function Reachouts() {
  const { data, isLoading, error } = useReachouts();
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('date_local');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const filteredSorted = useMemo(() => {
    if (!data) return [];
    const needle = search.trim().toLowerCase();
    const filtered = data.rows.filter((r) => matchesSearch(r, needle));
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const av = (a[sortKey] ?? '') as string;
      const bv = (b[sortKey] ?? '') as string;
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [data, search, sortKey, sortDir]);

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load reach-outs.</p>;

  const setSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  const columns: { key: SortKey; label: string }[] = [
    { key: 'recipient', label: 'Recipient' },
    { key: 'campaign', label: 'Campaign' },
    { key: 'persona', label: 'Persona' },
    { key: 'status', label: 'Status' },
    { key: 'date_local', label: 'Date' },
  ];

  return (
    <div>
      <h1>Reach-outs</h1>
      <div className={styles.toolbar}>
        <input
          type="search"
          className={styles.search}
          placeholder="Search recipient, company, domain, campaign…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <span className={styles.count}>
          {filteredSorted.length} of {data.total_count}
        </span>
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={col.key} onClick={() => setSort(col.key)}>
                  {col.label}
                  {sortKey === col.key && <span className={styles.arrow}>{sortDir === 'asc' ? '▲' : '▼'}</span>}
                </th>
              ))}
              <th>LinkedIn</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filteredSorted.length === 0 ? (
              <tr>
                <td colSpan={columns.length + 2} className={styles.empty}>
                  No reach-outs match this search.
                </td>
              </tr>
            ) : (
              filteredSorted.map((row) => (
                <RowActions key={row.recipient} row={row} colors={data.campaign_colors} />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
