import { useState } from 'react';
import { useCampaigns, useCampaignSlice, useFollowedUp, usePipeline, useReachouts } from '../api/hooks';
import type { FollowUpReminder } from '../api/types';
import { Card, CardGrid } from '../components/primitives/Card';
import { StatRow, StatTile } from '../components/primitives/StatTile';
import { CampaignDot } from '../components/primitives/Chip';
import { shortDate } from '../utils/format';
import styles from './Campaigns.module.css';

// One "aging" row in the Days-since-last-follow-up card. Carries its own
// "Followed up" action (same useFollowedUp mutation as Home's reminder rows —
// so a follow-up logged here resets days_since everywhere, atomically) and a
// LinkedIn marker when this lead has replied on LinkedIn.
function AgingRow({ lead, linkedin }: { lead: FollowUpReminder; linkedin: boolean }) {
  const followedUp = useFollowedUp(lead.recipient);
  return (
    <div className={styles.agingRow}>
      <span className={styles.agingWho}>
        {lead.recipient} {lead.company && `· ${lead.company}`}
        {linkedin && <span className={styles.inTag} title="Replied on LinkedIn">in</span>}
      </span>
      <span className={styles.agingRight}>
        <span className={styles.agingDays}>{lead.days_since}d</span>
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

  // Driven by follow_up_reminders (same real-tagged roster as active_leads,
  // but carrying the derived follow-up status: days_since + next_follow_up_date)
  // so each lead can show "how long since the last follow-up" and the next-nudge
  // date (req 4). Sorted most-recently-marked-real first — a "who's live" roster.
  const activeLeads = (pipeline?.follow_up_reminders ?? [])
    .filter(inCampaign)
    .slice()
    .sort((a, b) => (a.real_tagged_at < b.real_tagged_at ? 1 : -1));

  // Reach-outs in this campaign who have replied on LinkedIn (the OR-with-email
  // channel — surfaced here so a campaign's LinkedIn traction is visible).
  const linkedinReplied = (reachouts?.rows ?? []).filter((r) => r.linkedin_replied && inCampaign(r));
  // Fast membership test to flag LinkedIn replies inside the aging card.
  const linkedinSet = new Set(linkedinReplied.map((r) => r.recipient));

  // The same follow-up roster, re-ordered MOST OVERDUE first — a focused
  // "who's aging without a personal follow-up" view (days_since is derived from
  // the last touch, so it resets when "Followed up" is clicked).
  const followUpAging = (pipeline?.follow_up_reminders ?? [])
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
            followUpAging.map((l) => (
              <AgingRow key={l.recipient} lead={l} linkedin={linkedinSet.has(l.recipient)} />
            ))
          )}
        </Card>
      </CardGrid>
    </div>
  );
}
