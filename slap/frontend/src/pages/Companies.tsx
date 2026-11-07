import { useState } from 'react';
import { useCompanies, useCompanyTimeline } from '../api/hooks';
import type { CompanyNode, CompanyTimeline } from '../api/types';
import { shortDateTime } from '../utils/format';
// Same two-pane master/detail layout and timeline styling as Lifecycle.
import styles from './Lifecycle.module.css';

function Node({ node }: { node: CompanyNode }) {
  return (
    <li className={styles.node}>
      <span className={styles.nodeDot} data-chip={node.chip} />
      <div className={styles.nodeBody}>
        <div className={styles.nodeHead}>
          <span className={styles.nodeLabel}>{node.label}</span>
        </div>
        <div className={styles.nodeTime}>
          {shortDateTime(node.time)}
          {node.recipient ? ` · ${node.recipient}` : ' · posting'}
        </div>
        {node.detail && <div className={styles.nodeDetail}>{node.detail}</div>}
      </div>
    </li>
  );
}

function Detail({ data }: { data: CompanyTimeline }) {
  const meta = [
    `${data.leads.length} posting${data.leads.length === 1 ? '' : 's'}`,
    `${data.contacts.length} contact${data.contacts.length === 1 ? '' : 's'}`,
  ].join(' · ');
  return (
    <div>
      <div className={styles.charterHead}>
        <h2 className={styles.charterTitle}>{data.company}</h2>
        <div className={styles.charterMeta}>{meta}</div>
      </div>
      {data.timeline.length === 0 ? (
        <p className={styles.empty}>No events recorded yet.</p>
      ) : (
        <ol className={styles.timeline}>
          {data.timeline.map((n, i) => (
            <Node key={`${n.time}-${i}`} node={n} />
          ))}
        </ol>
      )}
    </div>
  );
}

export default function Companies() {
  const { data, isLoading, error } = useCompanies();
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const { data: detail, isLoading: detailLoading, error: detailError } = useCompanyTimeline(selected);

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load companies.</p>;

  const q = query.trim().toLowerCase();
  const rows = data.companies.filter((c) => !q || c.company.toLowerCase().includes(q));

  return (
    <div>
      <h1>Companies</h1>
      <div className={styles.split}>
        <div className={styles.rosterCol}>
          <input
            className={styles.search}
            type="search"
            placeholder="Search companies…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search companies"
          />
          <div className={styles.roster}>
            {rows.length === 0 ? (
              <p className={styles.empty}>No companies yet. Capture a posting in Claude Code (slap_capture).</p>
            ) : (
              rows.map((c) => (
                <button
                  key={c.company}
                  className={`${styles.rosterRow} ${selected === c.company ? styles.rosterRowActive : ''}`}
                  onClick={() => setSelected(c.company)}
                >
                  <span className={styles.rosterMain}>
                    <span className={styles.rosterEmail}>{c.company}</span>
                    <span className={styles.rosterCampaign}>
                      {c.leads} posting{c.leads === 1 ? '' : 's'} · {c.applied} applied · {c.contacts} contacted
                      {c.replied ? ` · ${c.replied} replied` : ''}
                    </span>
                  </span>
                </button>
              ))
            )}
          </div>
        </div>
        <div className={styles.charter}>
          {!selected ? (
            <p className={styles.empty}>Select a company to see its whole story.</p>
          ) : detailError ? (
            <p className={styles.empty}>Could not load this company's timeline: {String(detailError)}</p>
          ) : detailLoading || !detail ? (
            <p className={styles.empty}>Loading timeline…</p>
          ) : (
            <Detail data={detail} />
          )}
        </div>
      </div>
    </div>
  );
}
