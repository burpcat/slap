import './register';
import { Bar } from 'react-chartjs-2';
import type { ChartOptions } from 'chart.js';
import type { EngagementIntelligence } from '../api/types';
import type { EffectiveTheme } from './palette';
import { palette, sequentialRamp } from './palette';
import { ChartCard, chartCardStyles } from './ChartCard';

const BUCKET_LABELS: [keyof EngagementIntelligence['time_to_first_reply'], string][] = [
  ['same_day', 'Same day'],
  ['1_2_days', '1-2 days'],
  ['3_7_days', '3-7 days'],
  ['8_plus_days', '8+ days'],
];

/** Time-to-first-reply distribution -- an ORDINAL magnitude (fast -> slow),
 * not a categorical identity set, so this uses a single-hue sequential ramp
 * (light -> dark step over --series-1) rather than four unrelated
 * categorical hues -- the correct form for ordered buckets per the dataviz
 * skill's form heuristic. */
export function TimeToReplyChart({
  data,
  theme,
}: {
  data: EngagementIntelligence['time_to_first_reply'];
  theme: EffectiveTheme;
}) {
  const p = palette(theme);
  const ramp = sequentialRamp(theme, 4);
  const total = BUCKET_LABELS.reduce((sum, [key]) => sum + data[key], 0);

  if (total === 0) {
    return <p className={chartCardStyles.empty}>No replies with a recorded first-reply time yet.</p>;
  }

  const chartData = {
    labels: BUCKET_LABELS.map(([, label]) => label),
    datasets: [
      {
        label: 'Replies',
        data: BUCKET_LABELS.map(([key]) => data[key]),
        backgroundColor: ramp,
        borderRadius: 4,
      },
    ],
  };

  const options: ChartOptions<'bar'> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: p.textMuted }, grid: { display: false } },
      y: { ticks: { color: p.textMuted }, grid: { color: p.hairline }, beginAtZero: true },
    },
  };

  return (
    <ChartCard
      short
      chart={<Bar data={chartData} options={options} />}
      tableSummary="Table view"
      table={
        <table>
          <thead>
            <tr>
              <th>Bucket</th>
              <th>Replies</th>
            </tr>
          </thead>
          <tbody>
            {BUCKET_LABELS.map(([key, label]) => (
              <tr key={key}>
                <td>{label}</td>
                <td>{data[key]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    />
  );
}
