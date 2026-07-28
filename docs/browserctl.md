# browserctl

Thin human + agent CLI for **browser session leases** and **named profile lookup**.  
Navigators do **not** allocate raw CDP/profile resources; orchestrators/humans call `browserctl`, then spawn navigator with the returned `env`.

`identity_ops.py` remains **canonical** for xAI coal bind/start/stop.  
`browserctl` adapters wrap it (and scratch/vpn) behind one lease plane.

---

## Install / invoke

```bash
./bin/browserctl …           # preferred
./browserctl.py …            # root entry
python3 -m browserctl.cli …
```

Always safe for agents: add `--json`.

---

## Commands

| Cmd | Purpose |
|---|---|
| `list` | active leases (`--all` includes terminal) |
| `status --lease ID \| --worker W` | lease + live adapter + active-target |
| `acquire --kind xai\|scratch\|vpn …` | one mutation lease + start adapter |
| `release --lease ID` | stop resources; idempotent |
| `watch --lease ID [--agent-pane P]` | split navigator pane → observe_mirror |
| `unwatch --lease ID` | close watch pane; keep lease |
| `reap` / `reap --lease ID` | TTL / force cleanup |
| `mark-exit --lease ID` | navigator exited without release → expiring |
| `launch` / `spawn` | acquire (+ optional `--watch`) → navigator env JSON |
| `profiles list` | named profiles in `profiles/PROFILES.json` |
| `profiles show <name>` | one profile (launch + associations) |
| `profiles register <name> --kind …` | map name → launch selector |
| `profiles associate <name> <site> [account]` | site/account → profile |
| `profiles resolve <site> [--account …]` | exact resolve → `launch` + `launch_argv` |

### Scratch (demo / ad-hoc)

```bash
./bin/browserctl launch --kind scratch --label demo --owner orch --json
./bin/browserctl watch --lease "$LEASE" --agent-pane "$HERDR_PANE_ID" \
  --herdr-socket "$HERDR_SOCKET_PATH" --json
./bin/browserctl release --lease "$LEASE" --json
```

### xAI coal (identity_ops)

```bash
./bin/browserctl launch --kind xai --email 'USER@host' --owner orch --json
./bin/browserctl release --lease "$LEASE" --json
```

**Conflict safety:** xAI acquires mark `meta.attached_existing=true`. Duplicate lease → `LEASE_CONFLICT` and **never** stops the winner’s browser.

### VPN

```bash
./bin/browserctl acquire --kind vpn --worker vpn-se-sto --attach-only --json
./bin/browserctl acquire --kind vpn --worker vpn-se-sto --country Sweden --city Stockholm --json
```

### Named profiles

Registry: `profiles/PROFILES.json` (schema v1). No secrets. Selector is **persistent launch only** (`kind` + kind fields) — not lease `owner`/`mode`/`ttl`.

Resolve is **exact** (never fuzzy):

- with `--account`: only associations whose account equals it
- without `--account`: only **accountless** associations; if the site has only account-scoped rows → `PROFILE_NOT_FOUND` + hint to pass `--account`
- multiple matches → `PROFILE_AMBIGUOUS`
- emit-only (`launch` + `launch_argv`); never starts browsers; scratch never auto-created
- registry lock timeout → `PROFILE_LOCK_TIMEOUT` (not `LEASE_CONFLICT`)

```bash
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json
# → {"name","launch","launch_argv":["launch","--kind","xai","--email",…],…}

./bin/browserctl profiles register lab-vpn --kind vpn --worker vpn-se-sto --country Sweden --json
./bin/browserctl profiles register lab-scratch --kind scratch --label demo --json
./bin/browserctl profiles list --json
./bin/browserctl profiles show coal-demo --json
```

`register --replace` overwrites launch and keeps associations.

---

## Rules

1. **One mutation lease per worker.** Second `acquire` → `LEASE_CONFLICT` (exit 3).
2. **No managed `default`.** Coal and scratch refuse `default`.
3. **Atomic control files** under `state/control/` (leases, index, worker locks, ports lock).
4. **No secrets** in lease JSON, target-state, or `PROFILES.json`.
5. **Orchestrator owns the lease.** `release` in `finally`. Navigator exit → `mark-exit` / TTL `reap`.
6. **xAI conflict never kills the winner browser.**
7. **Scratch CDP ports** under global `ports.lock` with retry.
8. **Watch** requires exact herdr endpoint + observe_mirror-capable viewer root (fail closed).
9. **Profile resolve** is deterministic; refuse ambiguity; emit-only (no acquire).

---

## Watch / observe_mirror

### herdr endpoint (required — fail closed)

| Source | Vars / flags |
|---|---|
| CLI | `--herdr-socket PATH` · `--herdr-session NAME` |
| Env | `HERDR_SOCKET_PATH` · `HERDR_SESSION` |
| Lease | persisted on `lease.watch` after first successful watch |

Missing/ambiguous endpoint → `INVALID_REQUEST`.

### Viewer root

Resolve order (first capable match wins; explicit overrides fail closed if incapable):

| Priority | Source |
|---|---|
| 1 | `HERDR_BROWSER_ROOT` env |
| 2 | `BROWSERCTL_VIEWER_ROOT` env |
| 3 | repo-local `state/control/viewer-root` (single-line path; gitignored under `state/`) |
| 4 | built-in candidates (e.g. `/tmp/herdr-browser`) that pass the probe |

```bash
export HERDR_BROWSER_ROOT=/path/to/observe_mirror-capable/herdr-browser
# or:
export BROWSERCTL_VIEWER_ROOT=/path/to/observe_mirror-capable/herdr-browser
# or stable per-checkout (not committed):
mkdir -p state/control
echo '/path/to/observe_mirror-capable/herdr-browser' > state/control/viewer-root
```

Pre-merge worktrees OK if capability probe passes (`observe_mirror` + target-state markers). Fail closed otherwise. Do not hardcode operator home paths in-repo.

```bash
herdr pane split <agent-pane> --direction right --ratio 0.42 …
# HERDR_BROWSER_MODE=observe_mirror
# HERDR_BROWSER_TARGET_STATE=…/active-target.json
# HERDR_BROWSER_CDP_URL=http://127.0.0.1:<port>
```

---

## State layout

```text
state/control/
  index.json · index.lock · ports.lock
  leases/<lease_id>.json
  workers/<worker_id>/worker.lock

state/<worker>/control/
  active-target.json
  .active-target.json.lock

profiles/PROFILES.json          # tracked named selector registry
profiles/IDENTITIES.json        # runtime bind map (bootstrapped)
profiles/API_STAGES.json        # runtime stage ledger (bootstrapped)
profiles/.PROFILES.json.lock    # registry RMW (runtime)
state/control/viewer-root       # optional watch viewer path (runtime)
```

### active-target.json

Daemon publishes on connect / navigation / tab switch. Seq increments locked (monotonic).

---

## Persistent vs one-shot

| mode | TTL default | Who releases |
|---|---|---|
| `persistent` | 1h | orchestrator `release` |
| `one_shot` | 15m | same, shorter TTL |

```bash
./bin/browserctl mark-exit --lease "$LEASE" --json
./bin/browserctl reap --json
```

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 2 | not found / invalid / profile ambiguous |
| 3 | lease conflict |
| 1 | adapter / other error |
