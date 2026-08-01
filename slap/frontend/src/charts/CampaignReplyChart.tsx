import './register';
import { Bar } from 'react-chartjs-2';
import type { ChartOptions } from 'chart.js';
import type { EffectiveTheme } from './palette';
import { palette, categoricalColor } from './palette';
import { ChartCard, chartCardStyles } from './ChartCard';

/** Reply rate (%) by campaign -- the by-campaign twin of PersonaReplyChart.
 * Campaigns are an open-ended set (auto-discovered folders), so each is
 * colored by its sorted-name index into the SAME fixed token order every
 * other categorical chart on this page uses, exactly like the persona chart. */
export function CampaignReplyChart({
  data,
  theme,
}: {
  data: Record<string, number>;
  theme: EffectiveTheme;
}) {
  const p = palette(theme);
  const campaigns = Object.keys(data).sort();

  if (campaigns.length === 0) {
    return <p className={chartCardStyles.empty}>No campaign reply data yet.</p>;
  }

  const chartData = {
    labels: campaigns,
    datasets: [
      {
        label: 'Reply rate %',
        data: campaigns.map((name) => data[name]),
        backgroundColor: campaigns.map((_, i) => categoricalColor(theme, i)),
        borderRadius: 4,
      },
    ],
  };

  const options: ChartOptions<'bar'> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => `${ctx.formattedValue}%` } },
    },
    scales: {
      x: { ticks: { color: p.textMuted }, grid: { display: false } },
      y: { ticks: { color: p.textMuted, callback: (v) => `${v}%` }, grid: { color: p.hairline }, beginAtZero: true },
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
              <th>Campaign</th>
              <th>Reply rate</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((name) => (
              <tr key={name}>
                <td>{name}</td>
                <td>{data[name]}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    />
  );
}
