# Topology

Durable model of the checkout. Live truth: `./bin/browserctl list|status --json`, CDP `/json/version`, `state/<worker>/daemon.sock`.

## One sentence

A local control plane that leases Cloak processes and tabs. Drive a leased tab through the daemon unix socket.

## Layers

```
human / agent
  └─ browserctl (lease + named profile resolve)
       ├─ adapters
       │    ├─ scratch/adhoc  → unique or stable worker, cdp 9300–9399
       │    └─ vpn            → region worker / attach-only
       ├─ state/control/      → leases, ports, locks (no secrets)
       └─ daemon.sock
            └─ json-line {action, target_id, …}
                 └─ Cloak / Chromium
```

Cloak is the browser. The daemon holds CDP and runs rpc. `browserctl` is the mutex. `cloak` is bind + drive + release as one tool.

## Who owns what

| actor | owns | must not |
|---|---|---|
| human / orchestrator | `launch` / `release`, profile resolve, associate-after-login | driving pages; grabbing raw ports |
| agent (`cloak`) | page ops on an already-bound socket | allocating CDP/ports/profiles; attaching a second CDP client |
| browserctl | one process lease per worker; tab leases; adapter start/stop | secrets in lease/profile JSON |

## Happy path

CLI:

1. pick `--profile NAME` **or** `--kind scratch|vpn …`
2. `out=$(./bin/browserctl launch … --owner you --json)`
3. inspect `env.BROWSER_CDP_URL` and `env.BROWSERCTL_TARGET_ID` if you need to
4. `./bin/browserctl release --lease "$lease" --json`

Agent: `cloak action=bind` does 1–2; later acts are json-line on `state/<worker>/daemon.sock`. `cloak action=release` drops that tab only.

## Env

- `BROWSERCTL_LEASE_ID` — tab lease; release this
- `BROWSERCTL_TARGET_ID` — owned CDP page
- `BROWSERCTL_BROWSER_LEASE_ID`
- `BROWSER_HARNESS_WORKER`
- `BROWSER_CDP_URL` — humans/doctor; not a second driver
- `BROWSER_TARGET_STATE`
- `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE`
- `BROWSER_ALLOW_EVALUATE=1`
- `BROWSERCTL_PROFILE_NAME` — only via `--profile`

No env, no browser authority.

## Two registries (do not collapse them)

1. **PROFILES.json** (local, copy from `PROFILES.example.json`): named selectors. `register / associate / resolve / launch --profile`. no secrets. resolve is exact, never fuzzy, never starts browsers, never auto-creates scratch.
2. **Cloak user-data** under `profiles/scratch|vpn/…` (runtime, gitignored). cookies are the person.

## Named profiles

- `profiles register NAME --kind … --description …` maps name → launch selector
- `profiles associate NAME site [account]` only after a real login (never inferred from the live URL)
- `profiles resolve site [--account]` emits launch argv; accountless resolve only matches accountless rows
- `launch --profile NAME` exclusive with selector flags; stamps `lease.profile_name` + `BROWSERCTL_PROFILE_NAME`
- multi-match → `PROFILE_AMBIGUOUS`; missing → `PROFILE_NOT_FOUND`

Tracked demos (`profiles/PROFILES.example.json`):

- `scratch-general-1..3` — finite stable scratch pool (no associations)
- `shared-headed-demo` — stable scratch, `headed: true` (shared CDP, per-client tabs)

## Scratch: ephemeral vs stable

| shape | worker | wipe on release |
|---|---|---|
| ad-hoc `--kind scratch --label X` | unique token | yes if `ephemeral_wipe_v1` |
| `--worker` or named `--profile` scratch | stable | no |

## Lease laws

- one process lease per worker; many tab leases may share it
- one writer per tab; owned claim without `browserctl --steal` → `TARGET_CONFLICT`
- exclusive second process lease → `LEASE_CONFLICT`
- no managed `default` for leased work
- scheduled reap is the crash backstop for `one_shot` / `expiring` / `auto_reap`
- mutating drive requires a live `lease_id` (`TARGET_LEASE_REQUIRED`); socket steal is `STEAL_FORBIDDEN`
- control plane is atomic files under `state/control/`

```text
browser worker
├── tab lease A
└── tab lease B
```

## Cheat sheet

```bash
./bin/browserctl list --json
./bin/browserctl status --lease <id> --json
./bin/browserctl launch --kind scratch --label demo --owner you --json
./bin/browserctl launch --profile shared-headed-demo --owner you --json
./bin/browserctl release --lease <id> --json
./bin/browserctl profiles list --json
./bin/browserctl profiles resolve example.com --json
```
