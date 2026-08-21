# browser-ops topology brief

purpose: durable operator-model (not a session dump)

operator truth on disk:
- AGENTS.md (agent law)
- README.md (human entry)
- docs/browserctl.md (cli contract)
- docs/CONTRACT.md (drive-path law)
- profiles/BINDING.md (identity invariants)

live truth: `./bin/browserctl list|status --json`, CDP `/json/version`, `state/<worker>/daemon.sock`

---

## one-sentence system

browser-ops is a LOCAL control plane that leases Cloak processes and target tabs. omp navigators drive the leased tab only through the daemon unix socket. it does not spawn herdr panes.

## layered topology

```
human / orchestrator / cloak bind
  └─ browserctl (lease + named profile resolve)
       ├─ adapters
       │    ├─ scratch/adhoc  → unique or stable worker, cdp 9300-9399
       │    └─ vpn            → region worker / attach-only
       ├─ state/control/      → leases, ports, locks (no secrets)
       └─ daemon.sock
            └─ navigator (ONLY browser actor)
                 └─ cloak json-line {action, target_id, ...} on the socket
```

cloak is the browser substrate. daemon is the harness + rpc. browserctl is the mutex/lease desk. navigator is the hands via the `cloak` tool.

## ownership law

| actor | owns | must not |
|---|---|---|
| orchestrator / human | `launch`/`release`, profile resolve, associate-after-login | driving pages; raw port grabs |
| navigator | page ops via `cloak` on an already-bound daemon socket | allocating cdp/ports/profiles; lease lifecycle; shelling browserctl; attaching `app.cdp_url` |
| demiurge | code/config in browser-ops | browser mutation |
| browserctl | one browser/process lease per worker; target leases; adapter start/stop | secrets in lease/profile json |

## happy path

CLI (human / orchestrator):

1. decide selector: `--profile NAME` OR `--kind scratch|vpn …`
2. `out=$(./bin/browserctl launch … --owner <orch> --json)`
3. doctor may inspect `env.BROWSER_CDP_URL` **and** `env.BROWSERCTL_TARGET_ID`
4. `./bin/browserctl release --lease "$lease" --json`

OMP: `cloak action=bind` does steps 1–2; later `cloak` acts send json-line to `state/<worker>/daemon.sock` (`{action, target_id, ...}`). Navigators never open `app.cdp_url`. `release=true` / `cloak action=release` drops that target only.

## env contract

- `BROWSERCTL_LEASE_ID` (target lease; release this)
- `BROWSERCTL_TARGET_ID` (owned CDP page)
- `BROWSERCTL_BROWSER_LEASE_ID`
- `BROWSER_HARNESS_WORKER`
- `BROWSER_CDP_URL` (humans/doctor only — not navigator attach)
- `BROWSER_TARGET_STATE`
- `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE`
- `BROWSER_ALLOW_EVALUATE=1`
- `BROWSERCTL_PROFILE_NAME` (only via `--profile`)

no env, no browser authority. navigators still do not consume `BROWSER_CDP_URL`.

## two registries (do not collapse them)

1. **PROFILES.json** (local, copy from `PROFILES.example.json`): named SELECTORS only. `register / associate / resolve / launch --profile`. no secrets. resolve is exact, never fuzzy, never starts browsers, never auto-creates scratch.
2. **Cloak user-data** under `profiles/scratch|vpn/…` (runtime, gitignored). cookies are the person.

## named profile mechanics

- `profiles register NAME --kind … --description …` maps name → launch selector
- `profiles associate NAME site [account]` ONLY after proven login (never url-inferred)
- `profiles resolve site [--account]` emits launch argv; accountless resolve only matches accountless rows
- `launch --profile NAME` exclusive with selector flags; stamps `lease.profile_name` + `BROWSERCTL_PROFILE_NAME`
- multi-match → PROFILE_AMBIGUOUS; missing → PROFILE_NOT_FOUND

example tracked names (`profiles/PROFILES.example.json`):
- `scratch-general-1..3` → finite stable scratch pool (no associations)
- `shared-headed-demo` → stable scratch, `headed: true` (shared CDP, per-navigator targets)

## scratch: ephemeral vs stable

| shape | worker | wipe on release |
|---|---|---|
| ad-hoc `--kind scratch --label X` | unique token | YES if `ephemeral_wipe_v1` |
| `--worker` or named `--profile` scratch | stable | NO |

## lease laws

- one browser/process lease per worker; many target leases may share it
- one mutating owner per target; owned claim → TARGET_CONFLICT
- exclusive second browser lease → LEASE_CONFLICT
- no managed `default` for leased work
- scheduled reap is the crash backstop for `one_shot` / `expiring` / `auto_reap` (and stale targets)
- control plane atomic files under state/control/

```text
browser worker
├── target lease → navigator a
└── target lease → navigator b
```

## commands cheat sheet

```bash
./bin/browserctl list --json
./bin/browserctl status --lease <id> --json
./bin/browserctl launch --kind scratch --label demo --owner teach --json
./bin/browserctl launch --profile shared-headed-demo --owner teach --json
./bin/browserctl release --lease <id> --json
./bin/browserctl profiles list --json
./bin/browserctl profiles resolve example.com --json
```
