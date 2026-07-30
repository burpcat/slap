import { useState } from 'react';
import { Popover } from './primitives/Popover';
import { Button } from './primitives/Button';
import popoverStyles from './primitives/Popover.module.css';

/** Inline "resend to a corrected address" popover for a bounced reach-out
 * (replaces the same action that used to live in the row's ⋯ menu). Opens an
 * email input and calls onConfirm with the trimmed address; the caller wires
 * it to useResend(). Same anchored/portal Popover the OOO picker uses, so it
 * never clips near the bottom of the table. */
export function ResendPopover({
  trigger,
  onConfirm,
  pending,
}: {
  trigger: React.ReactNode;
  onConfirm: (correctedEmail: string) => void;
  pending?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState('');

  return (
    <Popover open={open} onOpenChange={setOpen} trigger={trigger}>
      <label className={popoverStyles.label}>Corrected address</label>
      <div className={popoverStyles.row}>
        <input
          type="email"
          className={popoverStyles.input}
          placeholder="corrected@address.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div className={popoverStyles.row}>
        <Button
          small
          variant="primary"
          disabled={pending || !email.trim()}
          onClick={() => {
            onConfirm(email.trim());
            setOpen(false);
          }}
        >
          Resend
        </Button>
        <Button small onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </Popover>
  );
}
