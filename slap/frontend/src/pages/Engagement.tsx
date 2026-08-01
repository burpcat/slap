import { useState } from 'react';
import { useEngagement, useHideWarmButSilent, useFollowedUp } from '../api/hooks';
import { useTheme } from '../theme/useTheme';
import type { WarmButSilentRow } from '../api/types';
import { Card, CardGrid } from '../components/primitives/Card';
import { Button } from '../components/primitives/Button';
import { Gauge } from '../components/primitives/StatTile';
import { RemindPopover } from '../components/RemindPopover';
import { Countdown } from '../components/Countdown';
import { PersonaBars } from '../components/PersonaBars';
import { shortDate } from '../utils/format';
import { TrendChart } from '../charts/TrendChart';
import { BounceChart } from '../charts/BounceChart';
import { PersonaReplyChart } from '../charts/PersonaReplyChart';
import { CampaignReplyChart } from '../charts/CampaignReplyChart';
import { TimeToReplyChart } from '../charts/TimeToReplyChart';
import styles from './Engagement.module.css';

// Days after the last click at which we suggest a personal nudge — the same
// display cadence the front-page follow-up reminders use (dashboard.FOLLOW_UP_
// NUDGE_DAYS). Kept here as a local display constant; nothing about delivery.
const NUDGE_DAYS = 7;

function WarmRow({ row, showingHidden }: { row: WarmButSilentRow; showingHidden: boolean }) {
  const hide = useHideWarmButSilent(row.recipient, true);
  const unhide = useHideWarmButSilent(row.recipient, false);
  const followedUp = useFollowedUp(row.recipient);

  // Most recent click drives both the "last clicked" stamp and the backwards
  // nudge timer (last click + NUDGE_DAYS). Clicks with no recorded time are
  // simply ignored here (never fabricated), same as everywhere else.
  const lastClickIso = row.clicks.reduce<string | null>(
    (latest, c) => (c.click_time && (!latest || c.click_time > latest) ? c.click_time : latest),
    null,
  );
  const nudgeTargetIso = lastClickIso
    ? new Date(new Date(lastClickIso).getTime() + NUDGE_DAYS * 86_400_000).toISOString()
    : null;
  const clickCount = row.clicks.length || row.stages_clicked.length;

  return (
    <div className={styles.row}>
      <div className={styles.rowMain}>
        <span className={styles.rowTitle}>{row.recipient}</span>
        <span className={styles.rowMeta}>
          {row.campaign} · clicked {clickCount} link{clickCount === 1 ? '' : 's'}, no reply yet
          {lastClickIso && ` · last clicked ${shortDate(lastClickIso)}`}
        </span>
        <Countdown targetIso={nudgeTargetIso} />
      </div>
      <div className={styles.actions}>
        <RemindPopover recipient={row.recipient} trigger={<Button small>Remind</Button>} />
        {/* "Followed up" appends the same interaction event the front-page
            reminders use, so a manual nudge here restarts that lead's
            follow-up timer too (req 6). */}
        <Button small disabled={followedUp.isPending} onClick={() => followedUp.mutate()}>
          Followed up
        </Button>
        {/* The payload has no per-row "currently hidden" flag -- in the
            show-hidden view we can't tell which rows are hidden vs visible,
            so both actions are offered there; in the default (visible-only)
            view every row present is, by definition, not hidden. */}
        {showingHidden && (
          <Button small disabled={unhide.isPending} onClick={() => unhide.mutate()}>
            Unhide
          </Button>
        )}
        <Button small disabled={hide.isPending} onClick={() => hide.mutate()}>
          Hide
        </Button>
      </div>
    </div>
  );
}

export default function Engagement() {
  const [showHidden, setShowHidden] = useState(false);
  const { data, isLoading, error } = useEngagement(showHidden);
  const { effective } = useTheme();

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load engagement data.</p>;

  const analytics = data['engagement-analytics'];

  return (
    <div>
      <h1>Engagement</h1>
      <CardGrid>
        <Card title="Reply rate by persona">
          {data.engagement.has_data ? (
            <PersonaBars rates={data.engagement.reply_rate_by_persona} />
          ) : (
            <p className={styles.empty}>No engagement data yet.</p>
          )}
        </Card>

        <Card title="Reply rate by campaign">
          {data.engagement.has_data ? (
            <PersonaBars rates={data.engagement.reply_rate_by_campaign} />
          ) : (
            <p className={styles.empty}>No engagement data yet.</p>
          )}
        </Card>

        <Card
          title={
            <>
              Warm but silent{' '}
              {data.warm_but_silent_hidden_count > 0 && (
                <button className={styles.hiddenToggle} onClick={() => setShowHidden((s) => !s)}>
                  {showHidden ? 'hide hidden' : `show hidden (${data.warm_but_silent_hidden_count})`}
                </button>
              )}
            </>
          }
          full
        >
          {data.warm_but_silent.length === 0 ? (
            <p className={styles.empty}>Nobody clicked without replying right now.</p>
          ) : (
            data.warm_but_silent.map((row) => <WarmRow key={row.recipient} row={row} showingHidden={showHidden} />)
          )}
        </Card>

        <Card title="Send / reply trend (30 days)" full>
          <TrendChart data={analytics.trend} theme={effective} />
        </Card>

        <Card title="Bounce &amp; block breakdown">
          <BounceChart data={analytics.bounce_data} theme={effective} />
        </Card>

        <Card title="Reply rate by persona (chart)">
          <PersonaReplyChart data={analytics.reply_rate_by_persona} theme={effective} />
        </Card>

        <Card title="Reply rate by campaign (chart)">
          <CampaignReplyChart data={analytics.reply_rate_by_campaign} theme={effective} />
        </Card>

        <Card title="Time to first reply">
          <TimeToReplyChart data={analytics.time_to_first_reply} theme={effective} />
        </Card>

        {analytics.weekly_goal && (
          <Card title="Weekly goal pacing">
            <div className={styles.goalWrap}>
              <div className={styles.rowMeta}>
                {analytics.weekly_goal.actual} / {analytics.weekly_goal.target} new sends this week (
                {analytics.weekly_goal.pct}%)
              </div>
              <Gauge
                pct={analytics.weekly_goal.pct}
                severity={analytics.weekly_goal.pct >= 100 ? 'good' : analytics.weekly_goal.pct >= 50 ? 'warning' : 'critical'}
              />
            </div>
          </Card>
        )}
      </CardGrid>
    </div>
  );
}
