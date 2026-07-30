import './register';
import { Bar } from 'react-chartjs-2';
import type { ChartOptions } from 'chart.js';
import type { BounceBreakdown } from '../api/types';
import type { EffectiveTheme } from './palette';
import { palette, bounceCategoryColors } from './palette';
import { ChartCard, chartCardStyles } from './ChartCard';
import styles from './BounceChart.module.css';

/** Weekly bounce vs block volume (stacked bar) plus the top bounce-reason
 * strings, all-time -- two categorical series (bounce/block), fixed
 * critical/serious colors matching the same meaning those chips carry
 * elsewhere on the dashboard. */
export function BounceChart({ data, theme }: { data: BounceBreakdown; theme: EffectiveTheme }) {
  const p = palette(theme);
  const colors = bounceCategoryColors(theme);
  const weeks = data.by_category_over_time;

  const chartData = {
    labels: weeks.map((w) => w.week_start),
    datasets: [
      { label: 'Bounced', data: weeks.map((w) => w.bounce), backgroundColor: colors.bounce, stack: 's' },
      { label: 'Blocked', data: weeks.map((w) => w.block), backgroundColor: colors.block, stack: 's' },
    ],
  };

  const options: ChartOptions<'bar'> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { position: 'top', labels: { color: p.textSecondary } },
      tooltip: { mode: 'index', intersect: false },
    },
    scales: {
      x: { stacked: true, ticks: { color: p.textMuted, maxTicksLimit: 8 }, grid: { display: false } },
      y: { stacked: true, ticks: { color: p.textMuted }, grid: { color: p.hairline }, beginAtZero: true },
    },
  };

  const hasVolume = weeks.some((w) => w.bounce > 0 || w.block > 0);

  return (
    <div>
      {hasVolume ? (
        <ChartCard
          short
          chart={<Bar data={chartData} options={options} />}
          tableSummary="Table view"
          table={
            <table>
              <thead>
                <tr>
                  <th>Week of</th>
                  <th>Bounced</th>
                  <th>Blocked</th>
                </tr>
              </thead>
              <tbody>
                {weeks.map((w) => (
                  <tr key={w.week_start}>
                    <td>{w.week_start}</td>
                    <td>{w.bounce}</td>
                    <td>{w.block}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        />
      ) : (
        <p className={chartCardStyles.empty}>No bounces or blocks recorded yet.</p>
      )}

      {data.top_reasons.length > 0 && (
        <div className={styles.reasonsWrap}>
          <h3 className={styles.reasonsTitle}>Top reasons</h3>
          <ul className={styles.reasonsList}>
            {data.top_reasons.map((r) => (
              <li key={r.reason} className={styles.reasonRow}>
                <span>{r.reason}</span>
                <span>{r.count}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
