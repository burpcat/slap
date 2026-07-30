import { useState } from 'react';
import { Popover } from './primitives/Popover';
import { Button } from './primitives/Button';
import { useRemind } from '../api/hooks';
import { ApiError } from '../api/client';
import popoverStyles from './primitives/Popover.module.css';

/** "Remind" action (Engagement's warm-but-silent list): the /remind endpoint
 * is being added on a separate track (see build brief) -- this posts
 * optimistically and degrades gracefully to a "coming soon" message on a
 * 404, rather than assuming the endpoint exists. */
export function RemindPopover({ recipient, trigger }: { recipient: string; trigger: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState('');
  const remind = useRemind(recipient);
  const notFound = remind.isError && remind.error instanceof ApiError && remind.error.status === 404;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) remind.reset();
      }}
      trigger={trigger}
    >
      {notFound ? (
        <p className={popoverStyles.label} style={{ margin: 0 }}>
          Reminders aren&apos;t available yet — coming soon.
        </p>
      ) : remind.isSuccess ? (
        <p className={popoverStyles.label} style={{ margin: 0 }}>Reminder queued.</p>
      ) : (
        <>
          <label className={popoverStyles.label}>New reminder note (optional)</label>
          <div className={popoverStyles.row}>
            <input
              className={popoverStyles.input}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. ping again re: role"
            />
          </div>
          <div className={popoverStyles.row}>
            <Button small variant="primary" disabled={remind.isPending} onClick={() => remind.mutate({ note })}>
              {remind.isPending ? 'Sending…' : 'Create new reminder'}
            </Button>
          </div>
        </>
      )}
    </Popover>
  );
}
