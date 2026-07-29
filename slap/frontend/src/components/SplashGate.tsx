import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import styles from './SplashGate.module.css';

const STORAGE_KEY = 'slap-splash-seen';

// Built letter-by-letter (each an equal-height row array) and zipped
// together with a fixed gap, rather than a single hand-typed block, so the
// column alignment can't silently drift out of a monospace grid.
const S = ['█████', '█    ', '█████', '    █', '█████'];
const L = ['█    ', '█    ', '█    ', '█    ', '█████'];
const A = [' ███ ', '█   █', '█████', '█   █', '█   █'];
const P = ['█████', '█   █', '█████', '█    ', '█    '];
const ASCII_ART = S.map((_, i) => [S[i], L[i], A[i], P[i]].join('   ')).join('\n');

function hasSeenSplash(): boolean {
  return window.localStorage.getItem(STORAGE_KEY) === '1';
}

export function clearSplashSeen() {
  window.localStorage.removeItem(STORAGE_KEY);
}

/** Full-screen ASCII splash (req 0), shown once per browser via localStorage,
 * dismissed by clicking Continue or pressing any key/Enter. Respects
 * prefers-reduced-motion (the CSS module's fade-in is disabled globally via
 * global.css's reduced-motion block; nothing here is animated beyond that). */
export function SplashGate({ children }: { children: ReactNode }) {
  const [dismissed, setDismissed] = useState(() => hasSeenSplash());

  const dismiss = () => {
    window.localStorage.setItem(STORAGE_KEY, '1');
    setDismissed(true);
  };

  useEffect(() => {
    if (dismissed) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      dismiss();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [dismissed]);

  if (dismissed) return <>{children}</>;

  return (
    <div className={styles.overlay}>
      <pre className={styles.ascii}>{ASCII_ART}</pre>
      <p className={styles.tagline}>cold outreach, tracked honestly.</p>
      <button className={styles.button} onClick={dismiss} autoFocus>
        Continue
      </button>
      <span className={styles.hint}>press any key</span>
    </div>
  );
}
