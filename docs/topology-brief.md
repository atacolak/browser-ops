# browser-ops topology brief

purpose: durable operator-model (not a session dump)

operator truth on disk:
- AGENTS.md (agent law)
- README.md (human entry)
- docs/browserctl.md (cli contract)
- profiles/BINDING.md (identity invariants)

live truth: `./bin/browserctl list|status --json`, CDP `/json/version`, `state/<worker>/control/active-target.json`

---

## one-sentence system

browser-ops is a LOCAL control plane that binds cloak profile dirs to cdp daemons and hands out a leased env (`BROWSER_CDP_URL`). it does not spawn herdr panes.

## layered topology

```
human / orchestrator / bind_profile
  └─ browserctl (lease + named profile resolve)
       ├─ adapters
       │    ├─ scratch/adhoc  → unique or stable worker, cdp 9300-9399
       │    └─ vpn            → region worker / attach-only
       ├─ state/control/      → leases, ports, locks (no secrets)
       └─ returns env JSON
            └─ navigator (ONLY browser actor)
                 ├─ browser open app.cdp_url
                 └─ daemon publishes active-target.json
```

cloak is the browser substrate. daemon is the harness + target publisher. browserctl is the mutex/lease desk. navigator is the hands.

## ownership law

| actor | owns | must not |
|---|---|---|
| orchestrator / human | `launch`/`release`, profile resolve, associate-after-login | driving pages; raw port grabs |
| navigator | page ops with ALREADY-ACQUIRED `cdp_url` | allocating cdp/ports/profiles; lease lifecycle; shelling browserctl |
| demiurge | code/config in browser-ops | browser mutation |
| browserctl | one mutation lease per worker; adapter start/stop | secrets in lease/profile json |

## happy path

1. decide selector: `--profile NAME` OR `--kind scratch|vpn …`
2. `out=$(./bin/browserctl launch … --owner <orch> --json)`
3. attach `env.BROWSER_CDP_URL`
4. `./bin/browserctl release --lease "$lease" --json`

OMP: `bind_profile` does steps 1–2; `browser` open uses `app.cdp_url`; `release=true` or session shutdown does step 4.

## env contract

- `BROWSERCTL_LEASE_ID`
- `BROWSER_HARNESS_WORKER`
- `BROWSER_CDP_URL`
- `BROWSER_TARGET_STATE`
- `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE`
- `BROWSER_ALLOW_EVALUATE=1`
- `BROWSERCTL_PROFILE_NAME` (only via `--profile`)

no env, no browser authority.

## two registries (do not collapse them)

1. **PROFILES.json** (tracked): named SELECTORS only. `register / associate / resolve / launch --profile`. no secrets. resolve is exact, never fuzzy, never starts browsers, never auto-creates scratch.
2. **Cloak user-data** under `profiles/scratch|vpn/…` (runtime, gitignored). cookies are the person.

## named profile mechanics

- `profiles register NAME --kind … --description …` maps name → launch selector
- `profiles associate NAME site [account]` ONLY after proven login (never url-inferred)
- `profiles resolve site [--account]` emits launch argv; accountless resolve only matches accountless rows
- `launch --profile NAME` exclusive with selector flags; stamps `lease.profile_name` + `BROWSERCTL_PROFILE_NAME`
- multi-match → PROFILE_AMBIGUOUS; missing → PROFILE_NOT_FOUND

current tracked names:
- `github-ata` → scratch label github-ata; assoc github.com / atacolak
- `scratch-general-1..3` → finite stable scratch pool (no associations)

## scratch: ephemeral vs stable

| shape | worker | wipe on release |
|---|---|---|
| ad-hoc `--kind scratch --label X` | unique token | YES if `ephemeral_wipe_v1` |
| `--worker` or named `--profile` scratch | stable | NO |

## lease laws

- one mutation lease per worker; duplicate → LEASE_CONFLICT
- no managed `default` for leased work
- scheduled reap is the crash backstop for `one_shot` / `expiring` / `auto_reap`
- control plane atomic files under state/control/

## commands cheat sheet

```bash
./bin/browserctl list --json
./bin/browserctl status --lease <id> --json
./bin/browserctl launch --kind scratch --label demo --owner teach --json
./bin/browserctl launch --profile github-ata --owner teach --json
./bin/browserctl release --lease <id> --json
./bin/browserctl profiles list --json
./bin/browserctl profiles resolve github.com --account atacolak --json
```
