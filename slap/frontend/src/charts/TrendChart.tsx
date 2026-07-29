import './register';
import { Line } from 'react-chartjs-2';
import type { ChartOptions } from 'chart.js';
import type { TrendPoint } from '../api/types';
import type { EffectiveTheme } from './palette';
import { palette, trendSeriesColors } from './palette';
import { ChartCard, chartCardStyles } from './ChartCard';

/** Daily new-sent / follow-up-sent / reply-count trend (30 days) --
 * categorical identity series (dataviz skill: "color follows the entity"),
 * fixed order every render regardless of any filter. */
export function TrendChart({ data, theme }: { data: TrendPoint[]; theme: EffectiveTheme }) {
  const p = palette(theme);
  const colors = trendSeriesColors(theme);

  const chartData = {
    labels: data.map((d) => d.date),
    datasets: [
      { label: 'New sent', data: data.map((d) => d.new), borderColor: colors.new, backgroundColor: colors.new, tension: 0.25, pointRadius: 0, borderWidth: 2 },
      { label: 'Follow-up sent', data: data.map((d) => d.follow_up), borderColor: colors.follow_up, backgroundColor: colors.follow_up, tension: 0.25, pointRadius: 0, borderWidth: 2 },
      { label: 'Replies', data: data.map((d) => d.replies), borderColor: colors.replies, backgroundColor: colors.replies, tension: 0.25, pointRadius: 0, borderWidth: 2 },
    ],
  };

  const options: ChartOptions<'line'> = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { position: 'top', labels: { color: p.textSecondary, usePointStyle: true } },
      tooltip: { mode: 'index', intersect: false },
    },
    scales: {
      x: { ticks: { color: p.textMuted, maxTicksLimit: 8 }, grid: { color: p.hairline } },
      y: { ticks: { color: p.textMuted }, grid: { color: p.hairline }, beginAtZero: true },
    },
  };

  if (data.every((d) => d.new === 0 && d.follow_up === 0 && d.replies === 0)) {
    return <p className={chartCardStyles.empty}>No send/reply activity in this window yet.</p>;
  }

  return (
    <ChartCard
      chart={<Line data={chartData} options={options} />}
      tableSummary="Table view"
      table={
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>New sent</th>
              <th>Follow-up sent</th>
              <th>Replies</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.date}>
                <td>{d.date}</td>
                <td>{d.new}</td>
                <td>{d.follow_up}</td>
                <td>{d.replies}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    />
  );
}
