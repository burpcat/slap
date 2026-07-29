import type { ButtonHTMLAttributes } from 'react';
import styles from './Button.module.css';

type Variant = 'default' | 'primary' | 'danger' | 'link';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  small?: boolean;
}

const VARIANT_CLASS: Record<Variant, string> = {
  default: '',
  primary: styles.primary,
  danger: styles.danger,
  link: styles.link,
};

export function Button({ variant = 'default', small, className, ...rest }: ButtonProps) {
  const cls = [styles.btn, VARIANT_CLASS[variant], small ? styles.small : '', className ?? '']
    .filter(Boolean)
    .join(' ');
  return <button className={cls} {...rest} />;
}
