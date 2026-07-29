import type { ReactNode } from 'react';
import styles from './Card.module.css';

export function CardGrid({ children }: { children: ReactNode }) {
  return <div className={styles.grid}>{children}</div>;
}

export function Card({
  title,
  full,
  children,
}: {
  title?: string;
  full?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={`${styles.card} ${full ? styles.full : ''}`}>
      {title && <h2 className={styles.title}>{title}</h2>}
      {children}
    </section>
  );
}
