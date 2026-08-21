# browserctl

Lease a Cloak browser. Named profiles live in `profiles/PROFILES.json` (copy from [`PROFILES.example.json`](../profiles/PROFILES.example.json)).

```bash
./bin/browserctl launch --kind scratch --label demo --owner you --json
./bin/browserctl release --lease "$LEASE" --json
```

Agents use the `cloak` tool (`omp/cloak.ts`) and drive the leased tab over the daemon unix socket. They do not shell this CLI and do not attach a second CDP client.

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
| `acquire --kind scratch\|vpn …` | start/join browser + mint tab lease |
| `release --lease ID` | stop resources; idempotent |
| `reap` / `reap --lease ID` | crash-backstop for auto-reap-eligible expired leases; `--lease` force |
| `mark-exit --lease ID` | mark a vanished lease expiring (TTL reap) |
| `launch` / `spawn` | acquire → env JSON (`BROWSER_CDP_URL`, lease id) |
| `launch\|acquire --profile NAME` | named-profile lookup → launch selector (+ lease stamp) |
| `launch\|acquire --target-id ID` | claim that chrome tab (join); `--steal` takes it from the current holder |
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
./bin/browserctl launch --kind scratch --label demo --owner you --json
./bin/browserctl release --lease "$LEASE" --json
```

| Launch shape | Worker | Wipe on successful release/reap |
|---|---|---|
| `--kind scratch` (optional `--label`) | unique token | yes if `ephemeral_wipe_v1` |
| `--kind scratch --worker W` / explicit `profile_dir` | stable | keep |
| `--profile NAME` (`scratch`/`adhoc`) | `launch.worker` or `scratch-profile-<name>` | keep (finite named pool) |

Wipe requires `resources.ephemeral_wipe_v1=true`. Legacy `ephemeral_profile` alone never wipes. Paths must sit under `profiles/scratch` and `<root>/state`.

Ports: 9300–9399, global `ports.lock` + retry.

---

## VPN

```bash
./bin/browserctl launch --kind vpn --country Sweden --owner you --json
./bin/browserctl launch --kind vpn --worker vpn-se-sto --attach-only --json
```

`--attach-only` binds an already-running region worker. Duplicate exclusive acquire on that worker is `LEASE_CONFLICT` and must **not** stop the winner.

---

## Named profiles

```bash
./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json
./bin/browserctl profiles resolve example.com --json
out=$(./bin/browserctl launch --profile lab-demo --owner you --json)
# lease.profile_name + env.BROWSERCTL_PROFILE_NAME=lab-demo
```

`--profile` **or** `--kind` required. `--profile` is exclusive with selector flags
(`--kind/--worker/--label/--cdp-port/--country/--city/--headed/--no-start/--attach-only`).
Runtime flags (`--owner/--mode/--ttl/--auto-reap/--target-id/--steal`) stay allowed.

Resolve is exact, never fuzzy, never starts browsers, never auto-creates scratch.
Accountless resolve matches only accountless rows. Multi-match → `PROFILE_AMBIGUOUS`.

`launch.egress` is `direct` or `{type:vpn,…}` — never a peer kind. scratch+egress vpn stays scratch.

---

## Env contract

| Key | Meaning |
|---|---|
| `BROWSERCTL_LEASE_ID` | tab lease id (release this) |
| `BROWSERCTL_BROWSER_LEASE_ID` | process lease |
| `BROWSERCTL_TARGET_ID` | owned CDP page |
| `BROWSERCTL_TARGET_LEASE_ID` | same as `BROWSERCTL_LEASE_ID` |
| `BROWSER_HARNESS_WORKER` | daemon worker id |
| `BROWSER_CDP_URL` | e.g. `http://127.0.0.1:9304` (inspect; not a second driver) |
| `BROWSER_TARGET_STATE` | `state/<worker>/control/active-target.json` |
| `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE` | checkout / state root |
| `BROWSER_ALLOW_EVALUATE=1` | daemon contract |
| `BROWSERCTL_PROFILE_NAME` | only when launched via `--profile` |

---

## Lease laws

1. **One process lease per worker.** Many tab leases may share that browser. At most one writer per tab.
2. Compatible second acquire/bind **joins** and allocates a new tab. Exclusive second process lease → `LEASE_CONFLICT`. Owned tab claim without `--steal` → `TARGET_CONFLICT`.
3. **No managed `default`.**
4. **No secrets** in lease JSON, target-state, `targets.json`, or `PROFILES.json`.
5. Persistent **process** leases stamp `expires_at` for observability; they are **not** auto-reaped. Tab leases with `--auto-reap` / `expiring` / `one_shot` are.
6. Auto-reap: `mode=one_shot`, `status=expiring`, or explicit `--auto-reap`. `reap --lease ID` force-bypasses. Reaping one tab does not stop the browser while siblings remain.
7. Attached/existing runtimes (vpn attach): join must not stop the winner.
8. **Tabs:** `tabs` is a census (`owned_by_me` / `owned_by` / `unowned`). `new_tab` mints a lease. `switch_tab` to a sibling peeks unless `steal=true`. `close_tab` only on your leases.

```text
browser worker
├── tab lease A
└── tab lease B
```

Two `cloak bind` / `launch` calls on the same named profile share one worker / daemon socket and get distinct tab leases. Drive is json-line `{action, target_id, …}` on `state/<worker>/daemon.sock`. Do not attach a second CDP client or adopt the first/visible tab. `release` drops that client's tab only.

Timer units: `packaging/systemd/user/` + `./bin/browserctl-reap-timer install`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | adapter / other error (`PROFILE_LOCK_TIMEOUT`, …) |
| 2 | usage / `InvalidRequest` / `PROFILE_NOT_FOUND` / `PROFILE_AMBIGUOUS` |
| 3 | `LEASE_CONFLICT` / `TARGET_CONFLICT` |
