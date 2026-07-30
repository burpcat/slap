import { useState } from 'react';
import { useCampaigns, useCampaignSlice, usePipeline } from '../api/hooks';
import { Card, CardGrid } from '../components/primitives/Card';
import { StatRow, StatTile } from '../components/primitives/StatTile';
import { CampaignDot } from '../components/primitives/Chip';
import { shortDate } from '../utils/format';
import styles from './Campaigns.module.css';

export default function Campaigns() {
  const { data, isLoading, error } = useCampaigns();
  const { data: pipeline } = usePipeline();
  const [selected, setSelected] = useState<string | null>(null);
  const { data: slice } = useCampaignSlice(selected);

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load campaigns.</p>;

  // Driven by follow_up_reminders (same real-tagged roster as active_leads,
  // but carrying the derived follow-up status: days_since + next_follow_up_date)
  // so each lead can show "how long since the last follow-up" and the next-nudge
  // date (req 4). Sorted most-recently-marked-real first — a "who's live" roster.
  const activeLeads = (pipeline?.follow_up_reminders ?? [])
    .filter((l) => !selected || l.campaign === selected)
    .slice()
    .sort((a, b) => (a.real_tagged_at < b.real_tagged_at ? 1 : -1));

  const totals = data.campaigns.reduce(
    (acc, c) => ({
      recipient_count: acc.recipient_count + c.recipient_count,
      reply_count: acc.reply_count + c.reply_count,
      click_count: acc.click_count + c.click_count,
      active_lead_count: acc.active_lead_count + c.active_lead_count,
    }),
    { recipient_count: 0, reply_count: 0, click_count: 0, active_lead_count: 0 },
  );

  const display = selected ? slice : totals;

  return (
    <div>
      <h1>Campaigns</h1>
      <div className={styles.selector}>
        <button
          className={`${styles.pill} ${!selected ? styles.pillActive : ''}`}
          onClick={() => setSelected(null)}
        >
          All campaigns
        </button>
        {data.campaigns.map((c) => (
          <button
            key={c.campaign}
            className={`${styles.pill} ${selected === c.campaign ? styles.pillActive : ''}`}
            onClick={() => setSelected(c.campaign)}
          >
            <CampaignDot color={c.color} />
            {c.campaign}
          </button>
        ))}
      </div>

      <CardGrid>
        <Card title={selected ? `${selected} — analytics` : 'All campaigns — analytics'} full>
          {display ? (
            <StatRow>
              <StatTile value={display.recipient_count} label="recipients" />
              <StatTile value={display.reply_count} label="replies" />
              <StatTile value={display.click_count} label="clicks" />
              <StatTile value={display.active_lead_count} label="active leads" />
            </StatRow>
          ) : (
            <p className={styles.empty}>Loading slice…</p>
          )}
        </Card>

        <Card title="Active leads marked real" full>
          {activeLeads.length === 0 ? (
            <p className={styles.empty}>No active leads yet{selected ? ` in ${selected}` : ''}.</p>
          ) : (
            activeLeads.map((lead) => (
              <div key={lead.recipient} className={styles.leadRow}>
                <span>
                  {lead.recipient} {lead.company && `· ${lead.company}`} {lead.role && `(${lead.role})`}
                </span>
                <span className={styles.leadStatus}>
                  <span className={styles.leadStatusMain}>
                    {lead.days_since}d since {lead.last_interaction_at ? 'last follow-up' : 'marked real'} · next
                    nudge {shortDate(lead.next_follow_up_date)}
                  </span>
                  <span className={styles.leadStatusSub}>
                    {lead.campaign} · marked real {shortDate(lead.real_tagged_at)}
                  </span>
                </span>
              </div>
            ))
          )}
        </Card>
      </CardGrid>
    </div>
  );
}
