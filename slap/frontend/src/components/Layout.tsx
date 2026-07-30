import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import { useTheme } from '../theme/useTheme';
import { useNav } from '../api/hooks';
import { SyncBanner } from './SyncBanner';
import styles from './Layout.module.css';

const NAV_ITEMS = [
  { to: '/', label: 'Home', end: true },
  { to: '/campaigns', label: 'Campaigns' },
  { to: '/engagement', label: 'Engagement' },
  { to: '/pipeline', label: 'Pipeline' },
  { to: '/reachouts', label: 'Reach-outs' },
  { to: '/commands', label: 'Commands' },
];

function ThemeToggle() {
  const { preference, cycle } = useTheme();
  const label = preference === null ? 'Auto' : preference === 'light' ? 'Light' : 'Dark';
  return (
    <button className={styles.themeToggle} onClick={cycle} title="Cycle theme: Auto -> Light -> Dark">
      {label}
    </button>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { data: nav } = useNav();
  const failureCount = nav?.template_failures_count ?? 0;

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <h1 className={styles.brand}>slap</h1>
        <nav className={styles.nav}>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navLinkActive : ''}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className={styles.headerRight}>
          <ThemeToggle />
        </div>
      </header>
      <SyncBanner runnerWarning={nav?.runner_staleness_warning ?? null} />
      <main className={styles.main}>{children}</main>
      <footer className={styles.footer}>
        <NavLink to="/logs">Logs</NavLink>
        <NavLink to="/template-failures">
          Template failures{failureCount > 0 && <span className={styles.badge}>{failureCount}</span>}
        </NavLink>
      </footer>
    </div>
  );
}
