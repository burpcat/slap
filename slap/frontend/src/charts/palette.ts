// Theme-aware hex palette for Chart.js canvases (which cannot read CSS custom
// properties). These hexes mirror src/tokens.css / slap/static/dashboard.css's
// `:root` values EXACTLY — see this repo's dataviz skill: "resolve the needed
// hex from a small theme-aware palette object keyed by light/dark," never
// read a CSS var inside canvas code. If dashboard.css's tokens ever change,
// update this file (and tokens.css) to match — do not invent new values here.
//
// Editorial note (see final report): this app's design system defines a
// SINGLE identity hue (--series-1) plus four fixed-meaning status colors
// (good/warning/serious/critical), not a dedicated categorical ramp. Reusing
// exactly those tokens for chart series (rather than inventing a separate,
// unvalidated categorical palette) keeps chart color meaning consistent with
// every chip/badge elsewhere in the app, at the cost of the categorical
// palette validator flagging a couple of adjacent-pair/contrast warnings
// (warning-vs-serious, and --serious's fixed hex being slightly out of the
// ideal per-mode lightness band since status colors don't shift with theme
// the way --series-1 does). Every chart below mitigates with a legend,
// direct/tooltip labels, and a <details> table-view twin, per the skill's
// "WARN obligates visible labels or a table view" rule.

export type EffectiveTheme = 'light' | 'dark';

interface Palette {
  seriesPrimary: string; // --series-1
  good: string;
  warning: string;
  serious: string;
  critical: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  hairline: string;
  surface: string;
}

const LIGHT: Palette = {
  seriesPrimary: '#2a78d6',
  good: '#0ca30c',
  warning: '#fab219',
  serious: '#ec835a',
  critical: '#d03b3b',
  textPrimary: '#0b0b0b',
  textSecondary: '#52514e',
  textMuted: '#898781',
  hairline: '#e1e0d9',
  surface: '#fcfcfb',
};

const DARK: Palette = {
  seriesPrimary: '#3987e5',
  // Status tokens are NOT redefined per-mode in dashboard.css — same hex in
  // both themes (see module comment above).
  good: '#0ca30c',
  warning: '#fab219',
  serious: '#ec835a',
  critical: '#d03b3b',
  textPrimary: '#ffffff',
  textSecondary: '#c3c2b7',
  textMuted: '#c3c2b7',
  hairline: '#2c2c2a',
  surface: '#1a1a19',
};

export function palette(theme: EffectiveTheme): Palette {
  return theme === 'dark' ? DARK : LIGHT;
}

/** Fixed categorical order for series that carry the "new/follow-up/replies"
 * identity — never cycled, never reassigned when a filter changes what's
 * visible (dataviz skill: "color follows the entity, never its rank"). */
export function trendSeriesColors(theme: EffectiveTheme) {
  const p = palette(theme);
  return { new: p.seriesPrimary, follow_up: p.warning, replies: p.good };
}

export function bounceCategoryColors(theme: EffectiveTheme) {
  const p = palette(theme);
  return { bounce: p.critical, block: p.serious };
}

/** A single-hue sequential ramp (light -> dark step) over --series-1, for the
 * ordinal time-to-first-reply buckets (same_day is "best"/fastest, 8+ days is
 * "worst"/slowest) -- sequential-by-magnitude, not categorical-by-identity,
 * so a single hue at varying opacity is the correct form (dataviz skill
 * choosing-a-form.md), and it sidesteps the categorical CVD/contrast checks
 * entirely. */
export function sequentialRamp(theme: EffectiveTheme, steps: number): string[] {
  const p = palette(theme);
  const hex = p.seriesPrimary;
  const alphas =
    steps === 4 ? [1, 0.78, 0.55, 0.35] : Array.from({ length: steps }, (_, i) => 1 - (i / steps) * 0.65);
  return alphas.map((a) => hexToRgba(hex, a));
}

/** Fixed order for an open-ended-but-small categorical set (e.g. per-persona
 * reply rate) -- assigns from the SAME token order every time, by index, so
 * "recruiter" is always the same color regardless of which other personas
 * are present in a given dataset. */
const CATEGORICAL_ORDER: (keyof Palette)[] = ['seriesPrimary', 'good', 'warning', 'serious', 'critical'];

export function categoricalColor(theme: EffectiveTheme, index: number): string {
  const p = palette(theme);
  const key = CATEGORICAL_ORDER[index % CATEGORICAL_ORDER.length];
  return p[key];
}

function hexToRgba(hex: string, alpha: number): string {
  const clean = hex.replace('#', '');
  const r = parseInt(clean.substring(0, 2), 16);
  const g = parseInt(clean.substring(2, 4), 16);
  const b = parseInt(clean.substring(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
