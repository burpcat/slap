import { useState } from 'react';
import {
  autoUpdate,
  flip,
  FloatingPortal,
  offset,
  shift,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from '@floating-ui/react';
import type { ReachoutRow } from '../api/types';
import { Button } from './primitives/Button';
import styles from './RowMenu.module.css';

type View = 'menu' | 'ooo' | 'resend';

export interface RowMenuActions {
  onMarkOoo: (resumeDate: string) => void;
  onStop: () => void;
  onResend: (correctedEmail: string) => void;
  onTagReal: () => void;
  onTagNotInterested: () => void;
  pending?: boolean;
}

/** Per-row "..." action menu (Reach-outs table) built with @floating-ui/react
 * and rendered in a FloatingPortal with flip/shift middleware, so it never
 * clips or renders offscreen -- the fix for the known "menu breaks after
 * search near the bottom of the viewport" bug: a portal escapes the table's
 * own stacking/overflow context entirely, and flip/shift repositions the
 * panel to stay on-screen regardless of where the trigger row lands. */
export function RowMenu({ row, actions }: { row: ReachoutRow; actions: RowMenuActions }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<View>('menu');
  const today = new Date().toISOString().slice(0, 10);
  const [resumeDate, setResumeDate] = useState(today);
  const [correctedEmail, setCorrectedEmail] = useState('');

  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: (next) => {
      setOpen(next);
      if (!next) setView('menu');
    },
    placement: 'bottom-end',
    whileElementsMounted: autoUpdate,
    middleware: [offset(6), flip({ padding: 8 }), shift({ padding: 8 })],
  });

  const click = useClick(context);
  const dismiss = useDismiss(context);
  const role = useRole(context, { role: 'menu' });
  const { getReferenceProps, getFloatingProps } = useInteractions([click, dismiss, role]);

  const close = () => setOpen(false);

  return (
    <>
      <button ref={refs.setReference} {...getReferenceProps()} className={styles.trigger} aria-label="Row actions">
        ⋯
      </button>
      {open && (
        <FloatingPortal>
          <div ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()} className={styles.panel}>
            {view === 'menu' && (
              <>
                {!row.stopped && (
                  <button
                    className={styles.item}
                    onClick={() => setView('ooo')}
                  >
                    Mark OOO…
                  </button>
                )}
                {row.status === 'bounced' && (
                  <button className={styles.item} onClick={() => setView('resend')}>
                    Resend to corrected address…
                  </button>
                )}
                <button
                  className={styles.item}
                  onClick={() => {
                    actions.onTagReal();
                    close();
                  }}
                >
                  Tag real
                </button>
                <button
                  className={styles.item}
                  onClick={() => {
                    actions.onTagNotInterested();
                    close();
                  }}
                >
                  Tag not interested
                </button>
                {!row.stopped && (
                  <>
                    <div className={styles.divider} />
                    <button
                      className={`${styles.item} ${styles.danger}`}
                      onClick={() => {
                        actions.onStop();
                        close();
                      }}
                    >
                      Stop outreach
                    </button>
                  </>
                )}
              </>
            )}

            {view === 'ooo' && (
              <>
                <div className={styles.backRow}>
                  <button className={styles.backBtn} onClick={() => setView('menu')}>
                    ← back
                  </button>
                  <span>Mark OOO</span>
                </div>
                <div className={styles.formRow}>
                  <input
                    type="date"
                    className={styles.input}
                    value={resumeDate}
                    min={today}
                    onChange={(e) => setResumeDate(e.target.value)}
                  />
                </div>
                <div className={styles.formRow}>
                  <Button
                    small
                    variant="primary"
                    disabled={actions.pending}
                    onClick={() => {
                      actions.onMarkOoo(resumeDate);
                      close();
                    }}
                  >
                    Confirm
                  </Button>
                </div>
              </>
            )}

            {view === 'resend' && (
              <>
                <div className={styles.backRow}>
                  <button className={styles.backBtn} onClick={() => setView('menu')}>
                    ← back
                  </button>
                  <span>Resend</span>
                </div>
                <div className={styles.formRow}>
                  <input
                    type="email"
                    className={styles.input}
                    placeholder="corrected@address.com"
                    value={correctedEmail}
                    onChange={(e) => setCorrectedEmail(e.target.value)}
                  />
                </div>
                <div className={styles.formRow}>
                  <Button
                    small
                    variant="primary"
                    disabled={actions.pending || !correctedEmail.trim()}
                    onClick={() => {
                      actions.onResend(correctedEmail.trim());
                      close();
                    }}
                  >
                    Send
                  </Button>
                </div>
              </>
            )}
          </div>
        </FloatingPortal>
      )}
    </>
  );
}
