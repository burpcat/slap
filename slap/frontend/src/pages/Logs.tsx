import { useLogs } from '../api/hooks';
import { Card, CardGrid } from '../components/primitives/Card';
import { StatusChip } from '../components/primitives/Chip';
import styles from './Logs.module.css';

const CHIP_COLOR: Record<string, string | null> = {
  'chip-good': 'good',
  'chip-serious': 'serious',
  'chip-critical': 'critical',
  'chip-neutral': null,
};

export default function Logs() {
  const { data, isLoading, error } = useLogs();

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load logs.</p>;

  return (
    <div>
      <h1>Logs</h1>
      <CardGrid>
        <Card title={`Events (${data.total_count}${data.truncated ? '+' : ''})`} full>
          {data.events.map((ev) => (
            <div key={ev.id} className={styles.eventRow}>
              <span className={styles.timestamp}>{new Date(ev.timestamp).toLocaleString()}</span>
              <StatusChip color={CHIP_COLOR[ev.display.chip] ?? null} label={ev.display.label} />
              <span>{ev.recipient ?? ''}</span>
              <span className={styles.detail}>{ev.display.detail}</span>
            </div>
          ))}
        </Card>

        {Object.entries(data.logs).map(([name, lines]) => (
          <Card key={name} title={name}>
            <pre className={styles.logPre}>{lines.length > 0 ? lines.join('\n') : '(empty)'}</pre>
          </Card>
        ))}
      </CardGrid>
    </div>
  );
}
