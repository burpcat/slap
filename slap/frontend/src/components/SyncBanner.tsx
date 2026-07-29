import { useGmassRefresh, useSyncStatus } from '../api/hooks';
import { Button } from './primitives/Button';
import styles from './SyncBanner.module.css';

function formatSyncedAt(iso: string | null): string {
  if (!iso) return 'never synced yet';
  return `last synced ${new Date(iso).toLocaleString()}`;
}

/** Persistent sync-status bar (req 1): last-synced time PROMINENTLY next to
 * a "Refresh now" button, plus the cache_status (stale/redis-unavailable)
 * states -- polled every ~60s via useSyncStatus's refetchInterval, same data
 * every GMass-dependent widget already reads (never a second, independent
 * sweep of its own). Also carries the nav-level runner-staleness warning
 * (a silently-stopped launchd job), same as base.html's global banner. */
export function SyncBanner({ runnerWarning }: { runnerWarning: string | null }) {
  const { data, isLoading } = useSyncStatus();
  const refresh = useGmassRefresh();

  const cacheStatus = data?.cache_status;
  const syncedAt = data?.sync_result.synced_at ?? null;

  return (
    <>
      <div className={styles.bar}>
        <div className={styles.left}>
          <span>{isLoading ? 'checking sync status…' : formatSyncedAt(syncedAt)}</span>
          {cacheStatus === 'stale_refreshing' && <span className={`${styles.status} ${styles.stale}`}>refreshing…</span>}
          {cacheStatus === 'redis_unavailable' && (
            <span className={`${styles.status} ${styles.unavailable}`}>cache unavailable (redis down)</span>
          )}
          {data && data.sync_result.errors.length > 0 && (
            <span className={`${styles.status} ${styles.unavailable}`}>
              {data.sync_result.errors.length} sync error(s)
            </span>
          )}
        </div>
        <Button
          small
          variant="primary"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
        >
          {refresh.isPending ? 'Refreshing…' : 'Refresh now'}
        </Button>
      </div>
      {runnerWarning && <div className={styles.warning}>{runnerWarning}</div>}
    </>
  );
}
