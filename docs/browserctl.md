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
| `acquire --kind scratch\|vpn …` | one mutation lease + start adapter |
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
| `BROWSERCTL_LEASE_ID` | lease id |
| `BROWSER_HARNESS_WORKER` | daemon worker id |
| `BROWSER_CDP_URL` | e.g. `http://127.0.0.1:9304` |
| `BROWSER_TARGET_STATE` | `state/<worker>/control/active-target.json` |
| `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE` | checkout / state root |
| `BROWSER_ALLOW_EVALUATE=1` | daemon contract |
| `BROWSERCTL_PROFILE_NAME` | only when launched via `--profile` |

---

## Lease laws

1. **One mutation lease per worker.** Duplicate → `LEASE_CONFLICT`.
2. **No managed `default`.**
3. **No secrets** in lease JSON, target-state, or `PROFILES.json`.
4. Persistent active leases stamp `expires_at` for observability; they are **not** auto-reaped.
5. Auto-reap only: `mode=one_shot`, `status=expiring`, or explicit `--auto-reap`. `reap --lease ID` force-bypasses.
6. Attached/existing runtimes (vpn attach): conflict must not stop the winner.

Timer units: `packaging/systemd/user/` + `./bin/browserctl-reap-timer install`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | adapter / other error (`PROFILE_LOCK_TIMEOUT`, …) |
| 2 | usage / `InvalidRequest` / `PROFILE_NOT_FOUND` / `PROFILE_AMBIGUOUS` |
