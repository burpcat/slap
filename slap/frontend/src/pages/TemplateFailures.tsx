import { useTemplateFailures } from '../api/hooks';
import { Card } from '../components/primitives/Card';
import styles from './Logs.module.css';

export default function TemplateFailures() {
  const { data, isLoading, error } = useTemplateFailures();

  if (isLoading) return <p>Loading…</p>;
  if (error || !data) return <p>Could not load template failures.</p>;

  return (
    <div>
      <h1>Template failures</h1>
      <Card full>
        {data.failures.length === 0 ? (
          <p>No template failures recorded.</p>
        ) : (
          data.failures.map((f, i) => (
            <div key={i} className={styles.eventRow}>
              <span>{f.campaign ?? f.file ?? 'unknown'}</span>
              <span className={styles.detail}>{f.error ?? JSON.stringify(f)}</span>
            </div>
          ))
        )}
      </Card>
    </div>
  );
}
