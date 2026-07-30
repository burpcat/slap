import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import styles from './ErrorBoundary.module.css';

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/** Catches render-time errors in the routed page so a single page's crash shows
 * a readable message in the content area instead of unmounting the whole app
 * (a thrown error with no boundary blanks everything — header included). The
 * surrounding Layout stays mounted, so the nav still works and the user can
 * move to another page. `resetKey` (the current path) clears the error on
 * navigation so a fixed/other route recovers without a full reload. */
export class ErrorBoundary extends Component<Props & { resetKey?: string }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prev: Props & { resetKey?: string }) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surfaced in the console for local debugging (this is a localhost tool).
    console.error('Page crashed:', error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className={styles.wrap}>
          <h2 className={styles.title}>This page hit an error.</h2>
          <p className={styles.body}>
            Try another tab, or reload. If it just started after an update, restart the dashboard
            server so its API matches the current page.
          </p>
          <pre className={styles.detail}>{this.state.error.message}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}
