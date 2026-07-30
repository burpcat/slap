import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { createElement } from 'react';

// Same 3-state cycle and localStorage key as the Jinja dashboard's
// theme-toggle script (dashboard_templates/base.html): null = Auto (follow
// the OS), 'light', 'dark'. Setting document.documentElement.dataset.theme
// is what tokens.css's `:root[data-theme="..."]` / `:root:not([data-theme=
// "light"])` selectors key off of — same mechanism, ported to React state.
export type ThemePreference = 'light' | 'dark' | null;
export type EffectiveTheme = 'light' | 'dark';

const STORAGE_KEY = 'slap-theme';

function readStoredPreference(): ThemePreference {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw === 'light' || raw === 'dark' ? raw : null;
}

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

interface ThemeContextValue {
  preference: ThemePreference;
  effective: EffectiveTheme;
  cycle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreference] = useState<ThemePreference>(() => readStoredPreference());
  const [systemDark, setSystemDark] = useState<boolean>(() => systemPrefersDark());

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const effective: EffectiveTheme = preference ?? (systemDark ? 'dark' : 'light');

  useEffect(() => {
    if (preference === null) {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = preference;
    }
  }, [preference]);

  const cycle = useCallback(() => {
    setPreference((prev) => {
      // Auto -> Light -> Dark -> Auto
      const next: ThemePreference = prev === null ? 'light' : prev === 'light' ? 'dark' : null;
      if (next === null) {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, next);
      }
      return next;
    });
  }, []);

  const value = useMemo(() => ({ preference, effective, cycle }), [preference, effective, cycle]);

  return createElement(ThemeContext.Provider, { value }, children);
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider');
  return ctx;
}
