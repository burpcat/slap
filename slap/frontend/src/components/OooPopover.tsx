import { useState } from 'react';
import { Popover } from './primitives/Popover';
import { Button } from './primitives/Button';
import popoverStyles from './primitives/Popover.module.css';

/** Shared OOO resume-date popover (req 2 on Home's triage actions, and the
 * Reach-outs row menu's "Mark OOO") -- opens a date input, POSTs
 * tag=ooo/resume_date on confirm via the caller's onConfirm. */
export function OooPopover({
  trigger,
  onConfirm,
  pending,
}: {
  trigger: React.ReactNode;
  onConfirm: (resumeDate: string) => void;
  pending?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const today = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(today);

  return (
    <Popover open={open} onOpenChange={setOpen} trigger={trigger}>
      <label className={popoverStyles.label}>Resume date</label>
      <div className={popoverStyles.row}>
        <input
          type="date"
          className={popoverStyles.input}
          value={date}
          min={today}
          onChange={(e) => setDate(e.target.value)}
        />
      </div>
      <div className={popoverStyles.row}>
        <Button
          small
          variant="primary"
          disabled={pending}
          onClick={() => {
            onConfirm(date);
            setOpen(false);
          }}
        >
          Confirm OOO
        </Button>
        <Button small onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </Popover>
  );
}
