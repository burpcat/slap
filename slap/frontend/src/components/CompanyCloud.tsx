import { useMemo } from 'react';
import { stripTld } from '../utils/format';
import styles from './CompanyCloud.module.css';

/** Front-page "word bomb" of every company contacted (req 3 — replaces the
 * ranked list + per-company counts). Each company's type size scales with how
 * many people we've contacted there, so density reads at a glance without a
 * number in sight. TLD is stripped for display; the count still drives size
 * and the title tooltip so the information isn't lost. */
export function CompanyCloud({ companies }: { companies: [string, number][] }) {
  const items = useMemo(() => {
    if (companies.length === 0) return [];
    const counts = companies.map(([, c]) => c);
    const min = Math.min(...counts);
    const max = Math.max(...counts);
    // Alphabetical by display label gives a stable, scannable cloud (no
    // reflow churn between renders); size — not order — encodes weight.
    return companies
      .map(([domain, count]) => {
        const t = max === min ? 1 : (count - min) / (max - min); // 0..1
        return { label: stripTld(domain), domain, count, weight: t };
      })
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [companies]);

  if (items.length === 0) {
    return <p className={styles.empty}>No companies contacted yet.</p>;
  }

  return (
    <div className={styles.cloud}>
      {items.map((it) => (
        <span
          key={it.domain}
          className={styles.word}
          title={`${it.domain} · ${it.count} contact${it.count === 1 ? '' : 's'}`}
          style={{
            // Size range ~0.85rem..1.9rem; opacity lifts heavier names forward.
            fontSize: `${0.85 + it.weight * 1.05}rem`,
            opacity: 0.55 + it.weight * 0.45,
          }}
        >
          {it.label}
        </span>
      ))}
    </div>
  );
}
