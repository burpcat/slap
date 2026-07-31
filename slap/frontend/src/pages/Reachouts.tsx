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
import { StatusChip, CampaignDot } from '../components/primitives/Chip';
import { OooPopover } from '../components/OooPopover';
import { ResendPopover } from '../components/ResendPopover';
import { ConfirmPopover } from '../components/ConfirmPopover';
import { shortDate } from '../utils/format';
import styles from './Reachouts.module.css';

type SortKey = 'recipient' | 'campaign' | 'persona' | 'status' | 'date_local';
type SortDir = 'asc' | 'desc';

// Client-side filter state, mirroring the backend's filter_reachouts()
// dimensions (slap/dashboard.py). Empty string / 'all' means "no constraint on
// this dimension", never "match nothing" — same semantics as filter_reachouts.
// Filtering is deliberately zero-network (all rows are already loaded), the
// same hard requirement the old Jinja page had.
interface Filters {
  campaign: string;
  persona: string;
  status: string;
  engagement: string;
  reply_tag: string;
  domain: string;
  reqId: 'all' | 'yes' | 'no';
  dateStart: string;
  dateEnd: string;
}

const EMPTY_FILTERS: Filters = {
  campaign: '',
  persona: '',
  status: '',
  engagement: '',
  reply_tag: '',
  domain: '',
  reqId: 'all',
  dateStart: '',
  dateEnd: '',
};

function distinct(rows: ReachoutRow[], key: keyof ReachoutRow): string[] {
  const seen = new Set<string>();
  for (const r of rows) {
    const v = r[key];
    if (typeof v === 'string' && v) seen.add(v);
  }
  return [...seen].sort();
}

function matchesSearch(row: ReachoutRow, needle: string): boolean {
  if (!needle) return true;
  const haystack = [row.recipient, row.company, row.name, row.domain, row.campaign, row.persona]
    .join(' ')
    .toLowerCase();
  return haystack.includes(needle);
}

function matchesFilters(row: ReachoutRow, f: Filters): boolean {
  if (f.campaign && row.campaign !== f.campaign) return false;
  if (f.persona && row.persona !== f.persona) return false;
  if (f.status && row.status !== f.status) return false;
  if (f.engagement && row.engagement !== f.engagement) return false;
  if (f.reply_tag && row.reply_tag !== f.reply_tag) return false;
  if (f.domain && row.domain !== f.domain) return false;
  if (f.reqId === 'yes' && !row.req_id_present) return false;
  if (f.reqId === 'no' && row.req_id_present) return false;
  // ISO YYYY-MM-DD compares correctly lexicographically (same format
  // date_local uses and <input type=date> produces) — no date parsing needed.
  if (f.dateStart && !(row.date_local && row.date_local >= f.dateStart)) return false;
  if (f.dateEnd && !(row.date_local && row.date_local <= f.dateEnd)) return false;
  return true;
}

function anyFilterActive(f: Filters): boolean {
  return (
    !!f.campaign || !!f.persona || !!f.status || !!f.engagement || !!f.reply_tag ||
    !!f.domain || f.reqId !== 'all' || !!f.dateStart || !!f.dateEnd
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <label className={styles.filterGroup}>
      <span className={styles.filterLabel}>{label}</span>
      <select className={styles.select} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

// The reply/engagement cell collapses email + LinkedIn into one "have they
// responded at all" read (req 9's OR of email replies and LinkedIn): an email
// reply and a LinkedIn reply are independent channels, either one counts as a
// response. Falls back to click / no-activity, and shows the owner's reply tag
// (real / OOO / not-interested) when one exists.
function ReplyCell({ row }: { row: ReachoutRow }) {
  const emailReplied = row.engagement === 'replied';
  const responded = emailReplied || row.linkedin_replied;
  return (
    <div className={styles.engage}>
      {responded ? (
        <span className={styles.responded}>
          replied
          <span className={styles.channels}>
            {emailReplied && 'email'}
            {emailReplied && row.linkedin_replied && ' + '}
            {row.linkedin_replied && 'in'}
          </span>
        </span>
      ) : row.engagement === 'clicked' ? (
        <span className={styles.clicked}>clicked</span>
      ) : (
        <span className={styles.noneEngage}>—</span>
      )}
      {row.reply_tag && <span className={styles.replyTag} data-tag={row.reply_tag}>{row.reply_tag.replace('_', ' ')}</span>}
    </div>
  );
}

function RowActions({ row, colors }: { row: ReachoutRow; colors: Record<string, CampaignColor> }) {
  const { effective } = useTheme();
  const tag = useTagReply(row.recipient);
  const stop = useStopOutreach(row.recipient);
  const resend = useResend(row.recipient);
  const linkedin = useLinkedinReplied(row.recipient);
  const color = colors[row.campaign];
  const tint = color ? (effective === 'dark' ? color.dark : color.light) : 'transparent';
  const pending = tag.isPending || stop.isPending || resend.isPending;

  const rowStyle = { borderLeftColor: tint } as CSSProperties;

  return (
    <tr className={styles.row} style={rowStyle} data-status={row.status}>
      <td>
        <div className={styles.recipientCell}>
          <span className={styles.recipientEmail}>{row.recipient}</span>
          {(row.name || row.company) && (
            <span className={styles.recipientMeta}>
              {[row.name, row.company].filter(Boolean).join(' · ')}
              {row.req_id_present && <span className={styles.reqId}>req id</span>}
            </span>
          )}
        </div>
      </td>
      <td>
        <span className={styles.campaignCell}>
          {color && <CampaignDot color={color} />}
          {row.campaign}
        </span>
      </td>
      <td>{row.persona}</td>
      <td>
        {/* Just the status here (Image #11) — the full bounce reason lives on
            the Pipeline page. chip.label for a bounced row is
            "Blocked — <full DSN>", so shorten it to the category word; every
            other status label is already short. Chip color is unchanged. */}
        <StatusChip
          color={row.chip.color}
          label={row.status === 'bounced' ? (row.bounce_category === 'block' ? 'Blocked' : 'Bounced') : row.chip.label}
        />
      </td>
      <td>
        <ReplyCell row={row} />
      </td>
      <td className={styles.dateCell}>{shortDate(row.date_local)}</td>
      <td>
        <div className={styles.actionsCell}>
          {/* Conditional recovery/halt actions come FIRST in the stack so the
              always-present triage buttons below stay pinned to the right edge:
              the cell is right-aligned, so a row that loses its Stop button no
              longer shifts Real/Not/OOO/in out of column alignment (Image #20). */}
          {!row.stopped && (row.status === 'active' || row.status === 'pending_retry') && (
            <ConfirmPopover
              message={`Stop all further outreach to ${row.recipient}?`}
              confirmLabel="Stop"
              pending={stop.isPending}
              onConfirm={() => stop.mutate()}
              trigger={<button className={styles.actBtn}>Stop</button>}
            />
          )}
          {row.status === 'bounced' && (
            <ResendPopover
              pending={resend.isPending}
              onConfirm={(corrected_email) => resend.mutate({ corrected_email })}
              trigger={<button className={styles.actBtn}>Resend</button>}
            />
          )}
          {/* Inline triage. Each button is neutral until it's the current tag,
              then it lights up in its own color (Image #13): Real=green,
              Not=red, OOO=orange. */}
          <button
            className={`${styles.actBtn} ${row.reply_tag === 'real' ? styles.selReal : ''}`}
            disabled={pending}
            onClick={() => tag.mutate({ tag: 'real' })}
          >
            Real
          </button>
          <button
            className={`${styles.actBtn} ${row.reply_tag === 'not_interested' ? styles.selNot : ''}`}
            disabled={pending}
            onClick={() => tag.mutate({ tag: 'not_interested' })}
          >
            Not
          </button>
          <OooPopover
            pending={pending}
            onConfirm={(resume_date) => tag.mutate({ tag: 'ooo', resume_date })}
            trigger={
              <button className={`${styles.actBtn} ${row.reply_tag === 'ooo' ? styles.selOoo : ''}`} disabled={pending}>
                OOO
              </button>
            }
          />
          {/* LinkedIn "in" is now a ONE-WAY gate (like Stop): marking replied-on-
              LinkedIn halts GMass outreach (status linkedin-gate), so it confirms
              first. Once gated it's a lit, disabled marker — no un-gate. */}
          {row.linkedin_gated ? (
            <button
              className={`${styles.linkedin} ${styles.linkedinActive}`}
              title="LinkedIn-gated — GMass outreach halted"
              disabled
            >
              in
            </button>
          ) : (
            <ConfirmPopover
              message={`Mark ${row.recipient} replied on LinkedIn and stop GMass outreach?`}
              confirmLabel="Mark & stop"
              pending={linkedin.isPending}
              onConfirm={() => linkedin.mutate({ replied: true })}
              trigger={
                <button
                  className={`${styles.linkedin} ${row.linkedin_replied ? styles.linkedinActive : ''}`}
                  title="Mark replied on LinkedIn (halts GMass outreach)"
                  disabled={linkedin.isPending}
                >
                  in
                </button>
              }
            />
          )}
        </div>
      </td>
    </tr>
  );
}

export default function Reachouts() {
  const { data, isLoading, error } = useReachouts();
  const [search, setSearch] = useState('');
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [sortKey, setSortKey] = useState<SortKey>('date_local');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const options = useMemo(() => {
    const rows = data?.rows ?? [];
    return {
      campaign: distinct(rows, 'campaign'),
      persona: distinct(rows, 'persona'),
      status: distinct(rows, 'status'),
      engagement: distinct(rows, 'engagement'),
      reply_tag: distinct(rows, 'reply_tag'),
      domain: distinct(rows, 'domain'),
    };
  }, [data]);

  const filteredSorted = useMemo(() => {
    if (!data) return [];
    const needle = search.trim().toLowerCase();
    const filtered = data.rows.filter((r) => matchesSearch(r, needle) && matchesFilters(r, filters));
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const av = (a[sortKey] ?? '') as string;
      const bv = (b[sortKey] ?? '') as string;
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [data, search, filters, sortKey, sortDir]);

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

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));
  const clearAll = () => {
    setFilters(EMPTY_FILTERS);
    setSearch('');
  };
  const filtersOn = anyFilterActive(filters) || !!search.trim();

  // Sortable columns that appear before the (non-sortable) Reply cell. Date is
  // rendered as its own sortable header after Reply so it sits beside the row's
  // date cell; Reply and Actions are not sortable.
  const columns: { key: SortKey; label: string }[] = [
    { key: 'recipient', label: 'Recipient' },
    { key: 'campaign', label: 'Campaign' },
    { key: 'persona', label: 'Persona' },
    { key: 'status', label: 'Status' },
  ];
  const TOTAL_COLS = columns.length + 3; // + Reply + Date + Actions

  return (
    <div className={styles.page}>
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
        {filtersOn && (
          <button className={styles.clear} onClick={clearAll}>
            Clear filters
          </button>
        )}
      </div>

      <div className={styles.filters}>
        <FilterSelect label="Campaign" value={filters.campaign} options={options.campaign}
          onChange={(v) => set({ campaign: v })} />
        <FilterSelect label="Persona" value={filters.persona} options={options.persona}
          onChange={(v) => set({ persona: v })} />
        <FilterSelect label="Status" value={filters.status} options={options.status}
          onChange={(v) => set({ status: v })} />
        <FilterSelect label="Engagement" value={filters.engagement} options={options.engagement}
          onChange={(v) => set({ engagement: v })} />
        <FilterSelect label="Reply tag" value={filters.reply_tag} options={options.reply_tag}
          onChange={(v) => set({ reply_tag: v })} />
        <FilterSelect label="Domain" value={filters.domain} options={options.domain}
          onChange={(v) => set({ domain: v })} />
        <label className={styles.filterGroup}>
          <span className={styles.filterLabel}>Req ID</span>
          <select className={styles.select} value={filters.reqId}
            onChange={(e) => set({ reqId: e.target.value as Filters['reqId'] })}>
            <option value="all">All</option>
            <option value="yes">Has req ID</option>
            <option value="no">No req ID</option>
          </select>
        </label>
        <label className={styles.filterGroup}>
          <span className={styles.filterLabel}>From</span>
          <input type="date" className={styles.date} value={filters.dateStart}
            onChange={(e) => set({ dateStart: e.target.value })} />
        </label>
        <label className={styles.filterGroup}>
          <span className={styles.filterLabel}>To</span>
          <input type="date" className={styles.date} value={filters.dateEnd}
            onChange={(e) => set({ dateEnd: e.target.value })} />
        </label>
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
              <th className={styles.noSort}>Reply</th>
              <th onClick={() => setSort('date_local')}>
                Date
                {sortKey === 'date_local' && <span className={styles.arrow}>{sortDir === 'asc' ? '▲' : '▼'}</span>}
              </th>
              <th className={styles.noSort}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredSorted.length === 0 ? (
              <tr>
                <td colSpan={TOTAL_COLS} className={styles.empty}>
                  No reach-outs match these filters.
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
