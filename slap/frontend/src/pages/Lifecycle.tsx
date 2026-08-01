import { useState } from 'react';
import { useReachouts, useCampaigns, useLifecycleDetail } from '../api/hooks';
import type { LifecycleDetail, LifecycleNode } from '../api/types';
import { CampaignDot } from '../components/primitives/Chip';
import { shortDateTime } from '../utils/format';
import styles from './Lifecycle.module.css';

// A reach-out's status dot color, reusing the same status vocabulary as the
// Reachouts chips (good/serious/critical), falling back to muted for neutral.
const DOT_COLOR: Record<string, string> = {
  good: 'var(--good)',
  serious: 'var(--serious)',
  critical: 'var(--critical)',
};

function TimelineNode({ node }: { node: LifecycleNode }) {
  return (
    <li className={`${styles.node} ${node.inferred ? styles.nodeInferred : ''}`}>
      <span className={styles.nodeDot} data-chip={node.chip} />
      <div className={styles.nodeBody}>
        <div className={styles.nodeHead}>
          <span className={styles.nodeLabel}>{node.label}</span>
          {node.inferred && (
            <span className={styles.inferredTag}>
              {node.scheduled ? 'scheduled via GMass' : '≈ sent via GMass'}
            </span>
          )}
          {node.stage != null && <span className={styles.stageBadge}>stage {node.stage}</span>}
        </div>
        <div className={styles.nodeTime}>{shortDateTime(node.time)}</div>
        {node.detail && <div className={styles.nodeDetail}>{node.detail}</div>}
      </div>
    </li>
  );
}

function Charter({ detail }: { detail: LifecycleDetail }) {
  return (
    <div>
      <div className={styles.charterHead}>
        <h2 className={styles.charterTitle}>{detail.recipient}</h2>
        <div className={styles.charterMeta}>
          {[detail.campaign, detail.persona, detail.status].filter(Boolean).join(' · ')}
        </div>
      </div>
      {detail.timeline.length === 0 ? (
        <p className={styles.empty}>No events recorded yet.</p>
      ) : (
        <ol className={styles.timeline}>
          {detail.timeline.map((n, i) => (
            <TimelineNode key={n.id ?? `inferred-${n.stage}-${i}`} node={n} />
          ))}
        </ol>
      )}
    </div>
  );
}

export default function Lifecycle() {
  const { data: reachouts, isLoading, error } = useReachouts();
  const { data: campaigns } = useCampaigns();
  const [selectedCampaign, setSelectedCampaign] = useState<string | null>(null);
  const [selectedRecipient, setSelectedRecipient] = useState<string | null>(null);
  const { data: detail, isLoading: detailLoading } = useLifecycleDetail(selectedRecipient);

  if (isLoading) return <p>Loading…</p>;
  if (error || !reachouts) return <p>Could not load reach-outs.</p>;

  const colors = reachouts.campaign_colors;
  const rows = reachouts.rows.filter((r) => !selectedCampaign || r.campaign === selectedCampaign);
  const pill = (active: boolean) => `${styles.pill} ${active ? styles.pillActive : ''}`;

  return (
    <div>
      <h1>Lifecycle</h1>
      <div className={styles.selector}>
        <button className={pill(!selectedCampaign)} onClick={() => setSelectedCampaign(null)}>
          All campaigns
        </button>
        {(campaigns?.campaigns ?? []).map((c) => (
          <button key={c.campaign} className={pill(selectedCampaign === c.campaign)} onClick={() => setSelectedCampaign(c.campaign)}>
            <CampaignDot color={c.color} />
            {c.campaign}
          </button>
        ))}
      </div>

      <div className={styles.split}>
        <div className={styles.roster}>
          {rows.length === 0 ? (
            <p className={styles.empty}>No reach-outs in this campaign.</p>
          ) : (
            rows.map((r) => (
              <button
                key={r.recipient}
                className={`${styles.rosterRow} ${selectedRecipient === r.recipient ? styles.rosterRowActive : ''}`}
                onClick={() => setSelectedRecipient(r.recipient)}
              >
                <span
                  className={styles.statusDot}
                  style={{ background: r.chip.color ? DOT_COLOR[r.chip.color] : 'var(--text-muted)' }}
                  title={r.chip.label}
                />
                <span className={styles.rosterMain}>
                  <span className={styles.rosterEmail}>{r.recipient}</span>
                  <span className={styles.rosterCampaign}>
                    {colors[r.campaign] && <CampaignDot color={colors[r.campaign]} />}
                    {r.campaign}
                  </span>
                </span>
              </button>
            ))
          )}
        </div>

        <div className={styles.charter}>
          {!selectedRecipient ? (
            <p className={styles.empty}>Select a reach-out to trace its lifecycle.</p>
          ) : detailLoading || !detail ? (
            <p className={styles.empty}>Loading timeline…</p>
          ) : (
            <Charter detail={detail} />
          )}
        </div>
      </div>
    </div>
  );
}
