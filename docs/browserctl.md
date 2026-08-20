# browserctl

Lease a Cloak browser. Named profiles live in `profiles/PROFILES.json`.

```bash
./bin/browserctl launch --kind scratch --label demo --owner orch --json
./bin/browserctl release --lease "$LEASE" --json
```

OMP navigators use `bind_profile` (source: `omp/bind-profile.ts`) and attach with `app.cdp_url`. They do not shell this CLI.

---

## Install / invoke

```bash
./bin/browserctl …
./browserctl.py …
python3 -m browserctl.cli …
```

Always safe for agents: add `--json`.

Root: `--root` → `BROWSER_OPS_ROOT` → this checkout. State: `--state-root` → `BROWSERCTL_STATE_ROOT` → `<root>/state`.

---

## Commands

| Cmd | Purpose |
|---|---|
| `list` | active leases (`--all` includes terminal) |
| `status --lease ID \| --worker W` | lease + live adapter + active-target |
| `acquire --kind scratch\|vpn …` | start/join browser + mint target lease |
| `release --lease ID` | stop resources; idempotent |
| `reap` / `reap --lease ID` | crash-backstop for auto-reap-eligible expired leases; `--lease` force |
| `mark-exit --lease ID` | mark a vanished lease expiring (TTL reap) |
| `launch` / `spawn` | acquire → env contract JSON (`BROWSER_CDP_URL`, lease id) |
| `launch\|acquire --profile NAME` | exact named-profile lookup → launch selector (+ lease stamp) |
| `profiles list` | named profiles in `profiles/PROFILES.json` |
| `profiles show <name>` | one profile (launch + associations) |
| `profiles register <name> --kind …` | map name → launch selector (description required on new) |
| `profiles associate <name> <site> [account]` | site/account → profile |
| `profiles resolve <site> [--account …]` | exact resolve → `launch` + `launch_argv` (does not start) |
| `profiles cards` / `card <name>` | markdown face cards |
| `profiles stamp <name>` | `last_verified_at = now` |

---

## Scratch

```bash
./bin/browserctl launch --kind scratch --label demo --owner orch --json
./bin/browserctl release --lease "$LEASE" --json
```

| Launch shape | Worker | Wipe on successful release/reap |
|---|---|---|
| `--kind scratch` (optional `--label`) | unique token | yes if `ephemeral_wipe_v1` |
| `--kind scratch --worker W` / explicit `profile_dir` | stable | keep |
| `--profile NAME` (`scratch`/`adhoc`) | `launch.worker` or `scratch-profile-<name>` | keep (finite named pool) |

Wipe requires `resources.ephemeral_wipe_v1=true`. Legacy `ephemeral_profile` alone never wipes. Paths must be contained under `profiles/scratch` and `<root>/state`.

Ports: 9300–9399, global `ports.lock` + retry.

---

## VPN

```bash
./bin/browserctl launch --kind vpn --country Sweden --owner orch --json
./bin/browserctl launch --kind vpn --worker vpn-se-sto --attach-only --json
```

`--attach-only` binds an already-running region worker. Duplicate acquire on that worker is `LEASE_CONFLICT` and must **not** stop the winner.

---

## Named profiles

```bash
./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json
./bin/browserctl profiles resolve example.com --json
out=$(./bin/browserctl launch --profile lab-demo --owner orch --json)
# lease.profile_name + env.BROWSERCTL_PROFILE_NAME=lab-demo
```

`--profile` **or** `--kind` required. `--profile` is exclusive with selector flags
(`--kind/--worker/--label/--cdp-port/--country/--city/--headed/--no-start/--attach-only`).
Runtime flags (`--owner/--mode/--ttl/--auto-reap`) stay allowed.

Resolve is exact, never fuzzy, never starts browsers, never auto-creates scratch.
Accountless resolve matches only accountless rows. Multi-match → `PROFILE_AMBIGUOUS`.

`launch.egress` is `direct` or `{type:vpn,…}` — never a peer kind. scratch+egress vpn stays scratch.

---

## Env contract

| Key | Meaning |
|---|---|
| `BROWSERCTL_LEASE_ID` | target lease id (release this) |
| `BROWSERCTL_BROWSER_LEASE_ID` | browser/process lease |
| `BROWSERCTL_TARGET_ID` | owned CDP target |
| `BROWSERCTL_TARGET_LEASE_ID` | same as `BROWSERCTL_LEASE_ID` |
| `BROWSER_HARNESS_WORKER` | daemon worker id |
| `BROWSER_CDP_URL` | e.g. `http://127.0.0.1:9304` |
| `BROWSER_TARGET_STATE` | `state/<worker>/control/active-target.json` |
| `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE` | checkout / state root |
| `BROWSER_ALLOW_EVALUATE=1` | daemon contract |
| `BROWSERCTL_PROFILE_NAME` | only when launched via `--profile` |

---

## Lease laws

1. **One browser/process lease per worker.** Many target leases may share that browser. At most one mutating owner per target.
2. Compatible second acquire/bind **joins** and allocates a new target. Exclusive second browser lease → `LEASE_CONFLICT`. Owned target claim → `TARGET_CONFLICT`.
3. **No managed `default`.**
4. **No secrets** in lease JSON, target-state, `targets.json`, or `PROFILES.json`.
5. Persistent **browser** leases stamp `expires_at` for observability; they are **not** auto-reaped. Target leases with `--auto-reap` / `expiring` / `one_shot` are.
6. Auto-reap: `mode=one_shot`, `status=expiring`, or explicit `--auto-reap`. `reap --lease ID` force-bypasses. Reaping one target does not stop the browser while siblings remain.
7. Attached/existing runtimes (vpn attach): join must not stop the winner.

```text
browser worker
├── target lease → navigator a
└── target lease → navigator b
```

Named-profile concurrency: two `bind_profile(profile=…)` calls share `BROWSER_CDP_URL` and get distinct `BROWSERCTL_TARGET_ID`s. OMP `browser open` must attach with `app.target_id` (or the bind sidecar / `BROWSERCTL_TARGET_ID`); it must not take the first or visible tab. `release=true` drops that navigator's target only.

Timer units: `packaging/systemd/user/` + `./bin/browserctl-reap-timer install`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | adapter / other error (`PROFILE_LOCK_TIMEOUT`, …) |
| 2 | usage / `InvalidRequest` / `PROFILE_NOT_FOUND` / `PROFILE_AMBIGUOUS` |
