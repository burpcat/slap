import { useState } from 'react';
import { useCampaigns, useCampaignSlice, useFollowedUp, usePipeline, useReachouts } from '../api/hooks';
import type { FollowUpAgingRow } from '../api/types';
import { Card, CardGrid } from '../components/primitives/Card';
import { StatRow, StatTile } from '../components/primitives/StatTile';
import { CampaignDot } from '../components/primitives/Chip';
import { shortDate } from '../utils/format';
import styles from './Campaigns.module.css';

// One "aging" row in the Days-since-last-follow-up card. Carries its own
// "Followed up" action (same useFollowedUp mutation as Home's reminder rows —
// so a follow-up logged here resets days_since and pushes next_follow_up_date
// forward everywhere, atomically) and a LinkedIn marker for LinkedIn leads.
function AgingRow({ lead }: { lead: FollowUpAgingRow }) {
  const followedUp = useFollowedUp(lead.recipient);
  return (
    <div className={styles.agingRow}>
      <span className={styles.agingWho}>
        {lead.recipient} {lead.company && `· ${lead.company}`}
        {lead.linkedin && <span className={styles.inTag} title="Replied on LinkedIn">in</span>}
      </span>
      <span className={styles.agingRight}>
        <span className={styles.agingDays}>
          {lead.days_since}d · next nudge {shortDate(lead.next_follow_up_date)}
        </span>
        <button
          className={styles.followedUpBtn}
          disabled={followedUp.isPending}
          onClick={() => followedUp.mutate()}
        >
          Followed up
        </button>
      </span>
    </div>
  );
}

export default function Campaigns() {
  const { data, isLoading, error } = useCampaigns();
  const { data: pipeline } = usePipeline();
  const { data: reachouts } = useReachouts();
  const [selected, setSelected] = useState<string | null>(null);
  const { data: slice } = useCampaignSlice(selected);

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load campaigns.</p>;

  const inCampaign = <T extends { campaign: string }>(x: T) => !selected || x.campaign === selected;

  // The plain "who's a live real lead" roster — just campaign + marked-real
  // date (the follow-up timing lives in the aging card below, not here).
  // Sorted most-recently-marked-real first.
  const activeLeads = (pipeline?.active_leads ?? [])
    .filter(inCampaign)
    .slice()
    .sort((a, b) => (a.real_tagged_at < b.real_tagged_at ? 1 : -1));

  // Reach-outs in this campaign who have replied on LinkedIn (the OR-with-email
  // channel — surfaced here so a campaign's LinkedIn traction is visible).
  const linkedinReplied = (reachouts?.rows ?? []).filter((r) => r.linkedin_replied && inCampaign(r));

  // The aging roster: a SUPERSET of the real leads that ALSO includes
  // LinkedIn-only leads (backend follow_up_aging), so everything needing a
  // personal nudge is in one place. Re-ordered MOST OVERDUE first; days_since
  // and the next-nudge date are derived from the last touch, so they reset when
  // "Followed up" is clicked.
  const followUpAging = (pipeline?.follow_up_aging ?? [])
    .filter(inCampaign)
    .slice()
    .sort((a, b) => b.days_since - a.days_since);

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
              <StatTile value={linkedinReplied.length} label="linkedin replies" />
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
                  <span className={styles.leadStatusSub}>
                    {lead.campaign} · marked real {shortDate(lead.real_tagged_at)}
                  </span>
                </span>
              </div>
            ))
          )}
        </Card>

        <Card title="Replied on LinkedIn" full>
          {linkedinReplied.length === 0 ? (
            <p className={styles.empty}>No LinkedIn replies yet{selected ? ` in ${selected}` : ''}.</p>
          ) : (
            linkedinReplied.map((r) => (
              <div key={r.recipient} className={styles.leadRow}>
                <span>
                  {r.recipient} {r.company && `· ${r.company}`} {r.name && `(${r.name})`}
                </span>
                <span className={styles.leadStatusSub}>
                  {r.campaign}
                  {r.linkedin_replied_at && ` · marked ${shortDate(r.linkedin_replied_at)}`}
                  {r.reply_tag && ` · ${r.reply_tag.replace('_', ' ')}`}
                </span>
              </div>
            ))
          )}
        </Card>

        <Card title="Days since last follow-up" full>
          {followUpAging.length === 0 ? (
            <p className={styles.empty}>No active leads to follow up{selected ? ` in ${selected}` : ''}.</p>
          ) : (
            followUpAging.map((l) => <AgingRow key={l.recipient} lead={l} />)
          )}
        </Card>
      </CardGrid>
    </div>
  );
}
