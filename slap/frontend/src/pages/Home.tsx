import { useHome, useTagReply, useFollowedUp } from '../api/hooks';
import type { ReplyNeedingTriage, FollowUpReminder } from '../api/types';
import { Card, CardGrid } from '../components/primitives/Card';
import { StatRow, StatTile, Gauge } from '../components/primitives/StatTile';
import { Button } from '../components/primitives/Button';
import { OooPopover } from '../components/OooPopover';
import { CompanyCloud } from '../components/CompanyCloud';
import { shortDate } from '../utils/format';
import styles from './Home.module.css';

function severityFor(pct: number): 'good' | 'warning' | 'critical' {
  if (pct >= 100) return 'critical';
  if (pct >= 75) return 'warning';
  return 'good';
}

function TriageRow({ reply }: { reply: ReplyNeedingTriage }) {
  const tag = useTagReply(reply.recipient);
  const hardWarning = reply.dedup_context.hard_warning;

  return (
    <div className={styles.row}>
      <div className={styles.rowMain}>
        <span className={styles.rowTitle}>{reply.recipient}</span>
        <span className={styles.rowMeta}>
          {reply.campaign} · stage {reply.stage} · {new Date(reply.timestamp).toLocaleString()}
          {hardWarning && ` · previously contacted (${hardWarning.status})`}
        </span>
      </div>
      <div className={styles.actions}>
        <Button small variant="primary" disabled={tag.isPending} onClick={() => tag.mutate({ tag: 'real' })}>
          Real
        </Button>
        <Button small disabled={tag.isPending} onClick={() => tag.mutate({ tag: 'not_interested' })}>
          Not real
        </Button>
        <OooPopover
          pending={tag.isPending}
          onConfirm={(resume_date) => tag.mutate({ tag: 'ooo', resume_date })}
          trigger={
            <Button small disabled={tag.isPending}>
              OOO
            </Button>
          }
        />
      </div>
    </div>
  );
}

function ReminderRow({ lead }: { lead: FollowUpReminder }) {
  const followedUp = useFollowedUp(lead.recipient);
  const everFollowedUp = !!lead.last_interaction_at;
  return (
    <div className={styles.row}>
      <div className={styles.rowMain}>
        <span className={styles.rowTitle}>
          {lead.recipient} {lead.company && `· ${lead.company}`}
        </span>
        <span className={styles.rowMeta}>
          {lead.campaign} · {lead.days_since}d since{' '}
          {everFollowedUp ? 'last follow-up' : 'marked real'} · next nudge {shortDate(lead.next_follow_up_date)}
        </span>
      </div>
      <div className={styles.actions}>
        <Button small variant="primary" disabled={followedUp.isPending} onClick={() => followedUp.mutate()}>
          Followed up
        </Button>
      </div>
    </div>
  );
}

export default function Home() {
  const { data, isLoading, error } = useHome();

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load the home dashboard.</p>;

  const { today, week, pipeline, companies, replies, follow_up_reminders } = data;

  return (
    <div>
      <h1 className={styles.pageTitle}>Home</h1>
      <CardGrid>
        <Card title="Today">
          <StatRow>
            <StatTile value={today.sent.total} label="sent (new + follow-up)" />
            <StatTile value={today.replies_today} label="replies" />
            <StatTile value={today.clicks_today} label="clicks" />
          </StatRow>
          <div style={{ marginTop: 16 }}>
            <div className={styles.rowMeta}>
              Daily cap: {today.cap_used_pct}% of {today.daily_cap}
            </div>
            <Gauge pct={today.cap_used_pct} severity={severityFor(today.cap_used_pct)} />
          </div>
        </Card>

        <Card title="This week">
          <StatRow>
            <StatTile value={week.sent.new} label="new sends" />
            <StatTile value={week.sent.follow_up} label="follow-ups" />
            <StatTile value={week.replies} label="replies" />
            <StatTile value={week.clicks} label="clicks" />
          </StatRow>
        </Card>

        <Card title="Replies needing triage" full>
          {replies.length === 0 ? (
            <p className={styles.empty}>Nothing waiting on triage.</p>
          ) : (
            replies.map((r) => <TriageRow key={r.recipient} reply={r} />)
          )}
        </Card>

        <Card title="Follow-up reminders">
          {follow_up_reminders.length === 0 ? (
            <p className={styles.empty}>No active leads waiting on a personal follow-up.</p>
          ) : (
            follow_up_reminders.map((lead) => <ReminderRow key={lead.recipient} lead={lead} />)
          )}
        </Card>

        <Card title="Pipeline summary">
          <StatRow>
            <StatTile
              value={Object.values(pipeline.mid_sequence_by_stage).reduce((sum, arr) => sum + arr.length, 0)}
              label="active, mid-sequence"
            />
            {/* "firing today" flips to "fired today" as the day's follow-ups
                drain: today.sent.follow_up is what GMass has already fired
                today, followups_scheduled.today is what's still due. */}
            <StatTile value={today.sent.follow_up} label="follow-ups fired today" />
            <StatTile value={pipeline.followups_scheduled.today.length} label="still firing today" />
            <StatTile value={pipeline.followups_scheduled.tomorrow.length} label="firing tomorrow" />
          </StatRow>
        </Card>

        <Card title="Companies contacted" full>
          <StatRow>
            <StatTile value={companies.all_time_count} label="all time" />
            <StatTile value={companies.this_week_count} label="this week" />
          </StatRow>
          <CompanyCloud companies={companies.all_companies} />
        </Card>
      </CardGrid>
    </div>
  );
}
