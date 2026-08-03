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
| `reap` / `reap --lease ID` | crash-backstop cleanup for **auto-reap-eligible** expired leases; `--lease` force |
| `mark-exit --lease ID` | navigator exited without release → expiring (auto-reap eligible) |
| `launch` / `spawn` | acquire (+ optional `--watch`) → navigator env JSON |
| `launch\|acquire --profile NAME` | exact named-profile lookup → launch selector (+ lease stamp) |
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

#### Ephemeral vs stable scratch

| Launch shape | Worker | `ephemeral_wipe_v1` | On successful `release` / `reap` |
|---|---|---|---|
| `--kind scratch` (optional `--label`) | unique token worker | `true` | wipe profile + state after processes dead + path containment |
| `--kind scratch --worker W` / explicit `profile_dir` | stable | absent/`false` | keep dirs |
| `--profile NAME` (`scratch`/`adhoc`) | `launch.worker` or `scratch-profile-<name>` | absent/`false` | keep (finite named pool) |

**Wipe requires `resources.ephemeral_wipe_v1=true`.** Legacy leases with only `ephemeral_profile=true` (main always stamped that, including explicit `--worker`) are **never** wiped. Request-side `ephemeral_profile` / `ephemeral_wipe_v1` cannot enable wiping. Additional gates: processes/CDP dead; `profile_dir` strict child of `profiles/scratch`; `state_dir` strict child of `<root>/state`; never control plane (`control_state_root` / `control/`).

Incomplete wipe → adapter `status=partial` + `cleanup_incomplete`; manager keeps lease `expiring` / `retryable` (not terminal) until cleanup succeeds.

Stale pre-upgrade dirs are not auto-pruned. Manual: list live workers via `browserctl list`, remove only unleased `profiles/scratch/<w>` + `state/<w>` after confirming no daemon/chrome.

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
# better: pin worker for a stable lab browser
./bin/browserctl profiles register lab-scratch --kind scratch --worker scratch-lab --label demo --json
./bin/browserctl profiles list --json
./bin/browserctl profiles show coal-demo --json
```

`register --replace` overwrites launch and keeps associations.

Named `scratch`/`adhoc` profiles are a **finite stable pool**: `--profile NAME` reuses
`launch.worker` or derived `scratch-profile-<name>` (no `ephemeral_wipe_v1`).
See [Ephemeral vs stable scratch](#ephemeral-vs-stable-scratch).

#### Launch / acquire by name (`--profile`)

Exact name lookup (`profiles show`); no site/account guessing on launch:

```bash
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
out=$(./bin/browserctl launch --profile coal-demo --owner orch --json)
# lease.profile_name + env.BROWSERCTL_PROFILE_NAME=coal-demo
./bin/browserctl release --lease "$(jq -r .lease.lease_id <<<"$out")" --json
```

`--profile` **or** `--kind` required. `--profile` is exclusive with selector flags
(`--kind/--email/--worker/--label/--cdp-port/--country/--city/--headed/--no-start/--attach-only`);
runtime flags (`--owner/--mode/--ttl/--watch/--ratio/--ready-timeout`) stay allowed. Manager enforces the same
exclusion and derives `headless` from profile `headed`.

#### Association learning (after successful login)

Registry associations are **operator/agent explicit** — not scraped from the live tab:

```bash
# only after a real successful login proved the binding
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles associate lab-scratch example.com --json   # accountless site
```

Strict selector exclusion on resolve: account filter is exact; accountless resolve never returns account-scoped rows; multi-match → `PROFILE_AMBIGUOUS`.

---

## Rules

1. **One mutation lease per worker.** Second `acquire` → `LEASE_CONFLICT` (exit 3).
2. **No managed `default`.** Coal and scratch refuse `default`.
3. **Atomic control files** under `state/control/` (leases, index, worker locks, ports lock).
4. **No secrets** in lease JSON, target-state, or `PROFILES.json`.
5. **Orchestrator owns the lease.** `release` in `finally`. Navigator exit → `mark-exit` (status `expiring`). Scheduled TTL `reap` is a crash backstop for auto-reap-eligible leases only (`one_shot` / `expiring` / `--auto-reap`) — not default persistent sessions past 1h.
6. **xAI conflict never kills the winner browser.**
7. **Scratch CDP ports** under global `ports.lock` with retry.
8. **Watch** requires exact herdr endpoint + observe_mirror-capable viewer root (fail closed); waits for CDP + non-null `active_target_id` present in `/json/list`; sets live resize + bounded 1:1 screencast; default **fixed** layout viewport 1150×902 (`HERDR_BROWSER_VIEWPORT_MODE=fixed`) so pane resize scales the frame without reflowing page layout; opt-in `--viewport follow-pane` for dynamic reflow; verifies viewer process then closes pane on failure; never seeds a null stub; default split ratio agent 37% / browser 63%.
9. **Profile resolve** is deterministic; refuse ambiguity; emit-only (no acquire).
10. **`--profile` launch** is exact name lookup; exclusive with selector flags; stamps `lease.profile_name` + `env.BROWSERCTL_PROFILE_NAME`.
11. **Association learning** is explicit: after a successful login to a new site/account, run `profiles associate <name> <site> [account]` — never invent associations from URL heuristics.
12. **Release/unwatch** clean up watch panes; orchestrator owns `finally`.
13. **Wipe only with `ephemeral_wipe_v1`** (new token workers); legacy `ephemeral_profile` alone never deletes dirs; named/explicit-worker scratches are stable.

---

## Watch / observe_mirror

### Cold-start readiness (fail closed)

`watch` **waits** until all settle, then splits the pane:

1. CDP `http://127.0.0.1:<port>/json/version` returns 200
2. `state/<worker>/control/active-target.json` has a **non-null** `active_target_id` (daemon publish)
3. That id exists in CDP `/json/list` (rejects stale published state)

It does **not** seed a null `active_target_id` stub. A null id is the harness cleared/cold state; opening observe_mirror on it freezes a permanent detached/`about:blank` mirror.

| Flag / knobs | Default |
|---|---|
| `--ready-timeout SEC` | `20` |
| poll interval | `0.25s` (internal) |

Timeout → `ADAPTER_ERROR` with bounded diagnostics (`cdp`, `cdp_list`, `target_state` summary, `reasons`, `hint`). No secrets in the payload.

### Viewer env + process verify

Watch pane env always includes:

| Var | Value | Why |
|---|---|---|
| `HERDR_BROWSER_MODE` | `observe_mirror` | mirror contract |
| `HERDR_BROWSER_TARGET_STATE` | active-target path | follow harness publish |
| `HERDR_BROWSER_CDP_URL` | lease CDP | attach |
| `HERDR_BROWSER_CAPTURE_BACKEND` | `screencast` | bound frames to capture raster instead of clipping oversized screenshots |
| `HERDR_BROWSER_CAPTURE_SCALE` | `1` | preserve 1:1 sharpness at the capture raster |
| `HERDR_BROWSER_VIEWPORT_MODE` | `fixed` (default) / `follow-pane` / `preserve` | layout policy for herdr-browser observe_mirror |
| `HERDR_BROWSER_VIEWPORT_WIDTH` | `1150` (fixed only) | real page layout width; measured fullscreen 1920×1080 herdr 37/63 browser pane |
| `HERDR_BROWSER_VIEWPORT_HEIGHT` | `902` (fixed only) | real page layout height at that measurement |
| `HERDR_BROWSER_FOLLOW_PANE_VIEWPORT` | `1` (**follow-pane only**) | legacy companion flag for older viewers; reflow page with terminal |
| `HERDR_BROWSER_VIEWER_WATCH_RESIZE` | `1` | enter live resize/graphics-stream loop (`shouldWatchResize`); without it daemon metrics stay `graphics_stream.active=false` / `frames=0` |

After `pane run`, watch polls herdr `pane process-info` (bounded ~3s) for `viewer.ts` / herdr-browser markers. Failure → close the newly split pane and raise `ADAPTER_ERROR` (`closed_on_failure=true`).

### Viewport mode (page layout vs frame scale)

| Mode | CLI | Env | Behavior |
|---|---|---|---|
| **fixed** (default) | `--viewport fixed` (or omit) | `VIEWPORT_MODE=fixed` + `WIDTH=1150` + `HEIGHT=902` | Apply real CSS layout once at 1150×902. Terminal/pane resize **contain-fits/scales** the rendered image; page responsive layout stays stable. |
| **follow-pane** | `--viewport follow-pane` | `VIEWPORT_MODE=follow-pane` + legacy `FOLLOW_PANE_VIEWPORT=1` | Page layout reflows with pane size (previous default). |
| **preserve** | `--viewport preserve` | `VIEWPORT_MODE=preserve` | Never mutate page layout (forensic/debug). |

Optional size overrides (fixed only): `--viewport-width` / `--viewport-height`.

```bash
# default: fixed 1150x902 layout; frame scales on pane resize
./bin/browserctl watch --lease "$LEASE" --agent-pane "$HERDR_PANE_ID" \
  --herdr-socket "$HERDR_SOCKET_PATH" --json

# opt-in dynamic page reflow
./bin/browserctl watch --lease "$LEASE" --viewport follow-pane --json

# launch/spawn with watch inherits the same flags
./bin/browserctl launch --kind scratch --label demo --watch \
  --viewport follow-pane --agent-pane "$HERDR_PANE_ID" \
  --herdr-socket "$HERDR_SOCKET_PATH" --json
```

Manager/API: `Manager.watch(..., viewport=, viewport_width=, viewport_height=)` and launch request keys `viewport` / `viewport_width` / `viewport_height` (for lifecycle `navigator spawn` integration later).

### Split ratio (herdr first-child fraction)

herdr `pane split --direction right --ratio R` keeps the **agent pane as first child (left)** and creates the watch pane as second (right). `R` is the first-child fraction, clamped to `[0.1, 0.9]`.

| | |
|---|---|
| **Default** | `0.37` → agent **37%** left / browser **63%** right |
| **Override** | `--ratio 0.4` (or any `(0,1)`) on `watch` / `launch --watch` |

```bash
./bin/browserctl watch --lease "$LEASE" --agent-pane "$HERDR_PANE_ID" \
  --herdr-socket "$HERDR_SOCKET_PATH" --json
# default ratio 0.37

./bin/browserctl watch --lease "$LEASE" --ratio 0.4 --ready-timeout 30 --json
```

### Cleanup

| Action | Effect |
|---|---|
| `unwatch --lease ID` | close watch pane; clear `lease.watch`; **keep** mutation lease |
| `unwatch --keep-pane` | unbind only; leave pane open |
| `release --lease ID` | stop adapter + **close watch pane** (unless `--keep-watch`) |
| `reap` | same release path for **auto-reap-eligible** expired leases / force ids |

Orchestrator owns cleanup: always `unwatch` or `release` in `finally`. Do not leave orphan observe_mirror panes. Scheduled `reap` is only a **crash backstop** (see below).

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
herdr pane split <agent-pane> --direction right --ratio 0.37 …
# HERDR_BROWSER_MODE=observe_mirror
# HERDR_BROWSER_TARGET_STATE=…/active-target.json
# HERDR_BROWSER_CDP_URL=http://127.0.0.1:<port>
# HERDR_BROWSER_VIEWER_WATCH_RESIZE=1
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

## Persistent vs one-shot + scratch profile policy

| mode | TTL default | Who releases | Scheduled `reap` after `expires_at` |
|---|---|---|---|
| `persistent` (default) | 1h | orchestrator `release` | **no** (unless `--auto-reap` or status `expiring`) |
| `one_shot` | 15m | orchestrator `release` in `finally` | **yes** (crash backstop) |

`expires_at` on persistent leases is observability / conflict messaging, **not** permission for the timer to kill the session. Live lab sessions often intentionally outlive the default 1h stamp.

### Auto-reap eligibility (scheduled `reap` / timer)

Blind TTL reap is unsafe: default `persistent` leases mark `expired` after 1h while still intentionally active. Scheduled `browserctl reap` only releases leases **intended** for automatic expiration:

| Eligible | Not eligible |
|---|---|
| `mode=one_shot` | `mode=persistent` + `status=active` without opt-in |
| `status=expiring` (`mark-exit`, incomplete wipe retry) | terminal `released` / `reaped` |
| explicit `auto_reap: true` (CLI `--auto-reap`, or legacy `meta.auto_reap`) | |

- `reap --lease ID` **force** still bypasses eligibility (operator scalpel).
- `reap --dry-run` lists what would be released under the same gate.
- Skipped ineligible expired leases appear in JSON `skipped[]` with `reason=not_auto_reap_eligible`.

**Normal path (fresh orchestrator):** `launch|spawn` → work → `release` in `finally`. Prefer `--mode one_shot` for finite navigator jobs so a crash still leaves an eligible lease for the backstop timer. You do **not** need the timer for happy-path cleanup.

**Crash backstop:** if the orchestrator dies without `release`, `one_shot` / `expiring` / `--auto-reap` leases are collected by periodic `reap`. Persistent active sessions are left alone.

### Scheduled timer (user systemd) — host admin, opt-in

Repo-owned templates: `packaging/systemd/user/browserctl-reap.{service,timer}.in`  
Helper (renders **absolute** `ROOT` into units, journal stdout/stderr):

```bash
# from the browser-ops checkout you want the timer to manage
./bin/browserctl-reap-timer install          # write units only; does NOT enable
./bin/browserctl-reap-timer print-units      # preview rendered unit text

# host admin — enable only when you intend the backstop on THIS machine
./bin/browserctl-reap-timer install --enable --now
# equivalent manual:
#   systemctl --user daemon-reload
#   systemctl --user enable --now browserctl-reap.timer

# inspect / logs
systemctl --user status browserctl-reap.timer browserctl-reap.service
journalctl --user -u browserctl-reap.service -n 50 --no-pager

# remove
./bin/browserctl-reap-timer uninstall --disable
```

| Safety | Detail |
|---|---|
| **Do not enable in worktree tasks** that share live control state with intentional long-lived persistent leases until you accept eligibility rules above | timer runs the same gated `reap --json` |
| Units bake **absolute** `WorkingDirectory` / `ExecStart` / `BROWSER_OPS_ROOT` | re-run `install` after moving the checkout |
| `Type=oneshot` + timer | no long-running reaper daemon |
| Default cadence | `OnBootSec=5m`, `OnUnitActiveSec=15m` |
| User systemd degraded? | fix linger/bus first; helper still writes units |

This tree’s task must **not** install/enable the timer or reap live state as part of landing the feature.

### Scratch: stable named vs ephemeral one-shot

| Intent | How |
|---|---|
| **Ephemeral demo** | `launch --kind scratch --label demo --mode one_shot` — unique `scratch-<label>-<token>` worker + profile dir; release tears down daemon/chrome |
| **Stable scratch profile** | `profiles register lab-scratch --kind scratch --label lab` then `launch --profile lab-scratch` — same **selector** every time; each acquire still gets a fresh worker token unless you pass a fixed `--worker` via profile launch fields |
| **Never** | `profiles resolve` must not auto-create scratch browsers or profile dirs |

Port allocator: scratch CDP ports (9300–9399) go through global `state/control/ports.lock` + `ports.json` reservations with retry on bind collision. Parallel acquires cannot steal the same port.

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
