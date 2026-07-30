import { useState } from 'react';
import { Popover } from './primitives/Popover';
import { Button } from './primitives/Button';
import popoverStyles from './primitives/Popover.module.css';

/** A tiny two-step confirm popover for a risky-but-reversible action (e.g. Stop
 * outreach) — warn, don't block: the action only fires after an explicit second
 * click, never on the first. Uses the same anchored/portal Popover as the OOO
 * and Resend pickers. */
export function ConfirmPopover({
  trigger,
  message,
  confirmLabel,
  onConfirm,
  pending,
}: {
  trigger: React.ReactNode;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  pending?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen} trigger={trigger}>
      <p className={popoverStyles.label}>{message}</p>
      <div className={popoverStyles.row}>
        <Button
          small
          variant="danger"
          disabled={pending}
          onClick={() => {
            onConfirm();
            setOpen(false);
          }}
        >
          {confirmLabel}
        </Button>
        <Button small onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </Popover>
  );
}
