import type { ReactNode } from 'react';
import styles from './ChartCard.module.css';

/** Wraps a canvas chart with a fixed-height container plus its mandatory
 * <details> table-view twin (dataviz skill: "every chart needs one," for
 * accessibility and for anyone who just wants the numbers). */
export function ChartCard({
  chart,
  tableSummary,
  table,
  short,
}: {
  chart: ReactNode;
  tableSummary: string;
  table: ReactNode;
  short?: boolean;
}) {
  return (
    <div>
      <div className={`${styles.container} ${short ? styles.short : ''}`}>{chart}</div>
      <details className={styles.tableView}>
        <summary>{tableSummary}</summary>
        {table}
      </details>
    </div>
  );
}

export { styles as chartCardStyles };
