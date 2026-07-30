import { usePipeline } from '../api/hooks';
import { Card, CardGrid } from '../components/primitives/Card';
import { BounceItem } from '../components/BounceItem';
import { shortDate } from '../utils/format';
import styles from './Pipeline.module.css';

export default function Pipeline() {
  const { data, isLoading, error } = usePipeline();

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load the pipeline.</p>;

  const stageEntries = Object.entries(data.pipeline.mid_sequence_by_stage).sort(
    ([a], [b]) => Number(a) - Number(b),
  );
  const { today, tomorrow } = data.pipeline.followups_scheduled;

  return (
    <div>
      <h1>Pipeline</h1>
      <CardGrid>
        {/* Companies contacted leads the grid so it lands in the first row
            (Image #19). */}
        <Card title="Companies contacted">
          <div className={styles.row}>
            <span>All time</span>
            <span>{data.companies.all_time_count}</span>
          </div>
          <div className={styles.row}>
            <span>This week</span>
            <span>{data.companies.this_week_count}</span>
          </div>
        </Card>

        <Card title="Mid-sequence, by current stage">
          {stageEntries.length === 0 ? (
            <p className={styles.empty}>Nobody currently mid-sequence.</p>
          ) : (
            <div className={styles.stageList}>
              {stageEntries.map(([stage, recipients]) => (
                <div key={stage} className={styles.stageTile}>
                  <div className={styles.stageValue}>{recipients.length}</div>
                  <div className={styles.stageLabel}>stage {stage}</div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="Follow-ups: fired vs firing">
          <div className={styles.stageList}>
            {/* Fired today climbs and "still firing today" falls as the day's
                follow-ups drain (req 2). */}
            <div className={styles.stageTile}>
              <div className={styles.stageValue}>{data.today.sent.follow_up}</div>
              <div className={styles.stageLabel}>fired today</div>
            </div>
            <div className={styles.stageTile}>
              <div className={styles.stageValue}>{today.length}</div>
              <div className={styles.stageLabel}>still firing today</div>
            </div>
            <div className={styles.stageTile}>
              <div className={styles.stageValue}>{tomorrow.length}</div>
              <div className={styles.stageLabel}>firing tomorrow</div>
            </div>
          </div>
        </Card>

        <Card title="Active leads" full>
          {data.active_leads.length === 0 ? (
            <p className={styles.empty}>No active leads marked real yet.</p>
          ) : (
            data.active_leads.map((lead) => (
              <div key={lead.recipient} className={styles.row}>
                <span>
                  {lead.recipient} {lead.company && `· ${lead.company}`}
                </span>
                <span>{lead.campaign}</span>
              </div>
            ))
          )}
        </Card>

        <Card title="Follow-up reminders">
          {data.follow_up_reminders.length === 0 ? (
            <p className={styles.empty}>Nothing overdue for a personal follow-up.</p>
          ) : (
            data.follow_up_reminders.map((r) => (
              <div key={r.recipient} className={styles.row}>
                <span>{r.recipient}</span>
                <span className={styles.reminderMeta}>
                  {r.days_since}d since {r.last_interaction_at ? 'follow-up' : 'real'} · next{' '}
                  {shortDate(r.next_follow_up_date)}
                </span>
              </div>
            ))
          )}
        </Card>

        <Card title="Bounces &amp; blocks (deliverability)" full>
          {data.bounces.length === 0 ? (
            <p className={styles.empty}>No bounces or blocks.</p>
          ) : (
            data.bounces.map((b) => <BounceItem key={b.recipient} bounce={b} />)
          )}
        </Card>

        <Card title="Stopped outreach roster" full>
          {data.stopped_outreach.length === 0 ? (
            <p className={styles.empty}>Nobody has been stopped.</p>
          ) : (
            data.stopped_outreach.map((s) => (
              <div key={s.recipient} className={styles.row}>
                <span>
                  {s.recipient} {s.company && `· ${s.company}`}
                </span>
                <span>
                  {s.stopped_at ? shortDate(s.stopped_at) : '—'} ({s.scope})
                </span>
              </div>
            ))
          )}
        </Card>
      </CardGrid>
    </div>
  );
}
