import { usePipeline } from '../api/hooks';
import { Card, CardGrid } from '../components/primitives/Card';
import { StatusChip } from '../components/primitives/Chip';
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

        <Card title="Follow-ups firing today / tomorrow">
          <div className={styles.stageList}>
            <div className={styles.stageTile}>
              <div className={styles.stageValue}>{today.length}</div>
              <div className={styles.stageLabel}>today</div>
            </div>
            <div className={styles.stageTile}>
              <div className={styles.stageValue}>{tomorrow.length}</div>
              <div className={styles.stageLabel}>tomorrow</div>
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
                <span>{r.days_since}d since real</span>
              </div>
            ))
          )}
        </Card>

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

        <Card title="Bounces &amp; blocks (deliverability)" full>
          {data.bounces.length === 0 ? (
            <p className={styles.empty}>No bounces or blocks.</p>
          ) : (
            data.bounces.map((b) => (
              <div key={b.recipient} className={styles.row}>
                <span>
                  {b.recipient} · {b.campaign}
                </span>
                <span>
                  <StatusChip color="critical" label={b.category === 'block' ? 'Blocked' : 'Bounced'} />{' '}
                  {b.reason}
                </span>
              </div>
            ))
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
                  {s.stopped_at ? new Date(s.stopped_at).toLocaleDateString() : '—'} ({s.scope})
                </span>
              </div>
            ))
          )}
        </Card>
      </CardGrid>
    </div>
  );
}
