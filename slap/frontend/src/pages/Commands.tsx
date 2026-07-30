import { useCommands } from '../api/hooks';
import { Card } from '../components/primitives/Card';
import { clearSplashSeen } from '../components/SplashGate';
import styles from './Commands.module.css';

export default function Commands() {
  const { data, isLoading, error } = useCommands();

  return (
    <div>
      <h1>Commands</h1>
      <a
        href="#"
        className={styles.replay}
        onClick={(e) => {
          e.preventDefault();
          clearSplashSeen();
          window.location.reload();
        }}
      >
        Replay intro
      </a>

      {isLoading && <p>Loading…</p>}
      {(error || !data) && !isLoading && <p>Could not load the command reference.</p>}

      {data?.commands.map((cmd) => (
        <Card key={cmd.name} full>
          <div className={styles.commandCard}>
            <p className={styles.name}>{cmd.name}</p>
            {cmd.help && <p className={styles.help}>{cmd.help}</p>}
            <pre className={styles.usage}>{cmd.usage}</pre>
            {/* Defensive: tolerate an older API payload that predates `examples`
                (e.g. a not-yet-restarted server) rather than throwing. */}
            {(cmd.examples ?? []).length > 0 && (
              <div className={styles.examples}>
                <span className={styles.examplesLabel}>Examples</span>
                {(cmd.examples ?? []).map((ex) => (
                  <code key={ex} className={styles.example}>
                    {ex}
                  </code>
                ))}
              </div>
            )}
            {cmd.args.length > 0 && (
              <table className={styles.argsTable}>
                <thead>
                  <tr>
                    <th>Arg</th>
                    <th>Flags</th>
                    <th>Required</th>
                    <th>Choices</th>
                    <th>Help</th>
                  </tr>
                </thead>
                <tbody>
                  {cmd.args.map((arg) => (
                    <tr key={arg.name}>
                      <td>{arg.name}</td>
                      <td>{arg.flags.join(', ') || '—'}</td>
                      <td>{arg.required ? 'yes' : 'no'}</td>
                      <td>{arg.choices ? arg.choices.join(', ') : '—'}</td>
                      <td>{arg.help}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </Card>
      ))}
    </div>
  );
}
