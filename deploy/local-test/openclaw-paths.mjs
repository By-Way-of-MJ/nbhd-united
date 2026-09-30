// OpenClaw 2026.9.1 hardcodes /tmp for lifecycle SQLite coordinator files,
// ignoring TMPDIR, and reads its own start time via setuid /bin/ps (blocked by the
// sandbox). Adapt just those installed modules in this process so all writes stay
// inside the approved test home/worktree. Never edit shared runtime.
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';

// The launcher sets TMPDIR under this checkout's .state, wherever it lives.
const stateDir = fileURLToPath(new URL('./.state/', import.meta.url));
if (!process.env.TMPDIR?.startsWith(stateDir)) {
  throw new Error('OpenClaw test launcher requires its isolated TMPDIR');
}
registerHooks({
  load(url, context, nextLoad) {
    const loaded = nextLoad(url, context);
    if (url.startsWith('file:') && /\/state-database-coordinator-[^/]+\.js$/.test(fileURLToPath(url))) {
      const source = String(loaded.source);
      const needle = ': "/tmp";';
      if (source.split(needle).length !== 2) {
        throw new Error('Installed OpenClaw lifecycle path changed; review the local path adapter');
      }
      return { ...loaded, source: source.replace(needle, ': process.env.TMPDIR;') };
    }
    // macOS Seatbelt (gateway.sb) refuses to exec the setuid /bin/ps, so OpenClaw cannot read its own
    // start time and the cron engine fails to start ("durable fence without process start identity").
    // Answer for this process from its own clock (same whole-second precision as `ps -o lstart=`).
    if (url.startsWith('file:') && /\/pid-alive-[^/]+\.js$/.test(fileURLToPath(url))) {
      const source = String(loaded.source);
      const needle = 'function getDarwinProcessStartTime(pid) {\n';
      if (source.split(needle).length !== 2) {
        throw new Error('Installed OpenClaw process-identity code changed; review the local path adapter');
      }
      return {
        ...loaded,
        source: source.replace(
          needle,
          needle + '\tif (pid === process.pid) return Math.floor((Date.now() - process.uptime() * 1000) / 1000);\n',
        ),
      };
    }
    return loaded;
  },
});
