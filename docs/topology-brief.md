# browser-ops topology brief
generated: 2026-07-31
purpose: durable operator-model for iterative teaching (not a novel; not a dump of the 16mb session export)
source session: pi-session-2026-07-28T04-06-49-471Z_019fa6e7-617f-7096-96df-cdc83ce41383.html
repo: /home/sf/workspace/browser-ops @ main (df21ab6)

## how to use this brief
teach from THIS file + live commands. do not ingest the html export.
operator truth on disk:
- AGENTS.md (agent law)
- README.md (human entry)
- docs/browserctl.md (cli contract)
- profiles/BINDING.md (identity invariants)
live truth: `./bin/browserctl list|status --json`, `python3 identity_ops.py list`, CDP `/json/version`, `state/<worker>/control/active-target.json`

success = user can predict the fleet loop, name who owns what, and invent next fleet moves without cargo-culting flags.

---

## one-sentence system
browser-ops is a LOCAL control plane that binds cloak browser profiles to cdp daemons, hands navigators a leased env (never raw port allocation), and can mirror the live page into a herdr pane beside the agent.

## layered topology (outside → inside)

```
human / orchestrator
  └─ browserctl (lease + named profile resolve)
       ├─ adapters
       │    ├─ scratch/adhoc  → unique or stable worker, cdp 9300-9399
       │    ├─ xai            → identity_ops ensure/stop, cdp 9223-9299
       │    └─ vpn            → region worker / attach-only
       ├─ state/control/      → leases, ports, locks, viewer-root (no secrets)
       └─ returns env JSON
            └─ navigator pane (ONLY browser actor)
                 ├─ talks to daemon via BROWSER_HARNESS_WORKER + cdp
                 ├─ daemon publishes active-target.json
                 └─ optional browserctl watch → herdr observe_mirror pane (read-only pixels)
```

cloak/chrome is the browser substrate. daemon is the harness + target publisher. browserctl is the mutex/lease desk. navigator is the hands. orchestrator is the leash-holder.

## ownership law (load-bearing)
| actor | owns | must not |
|---|---|---|
| orchestrator / human | `browserctl launch/acquire/watch/release`, profile resolve, identity retire/stage decisions | driving pages; raw port grabs for coal |
| navigator | page ops with ALREADY-ACQUIRED env; domain skills | allocating cdp/ports/profiles; lease lifecycle |
| demiurge | code/config in browser-ops | browser mutation |
| identity_ops | canonical xai bind map + stage ledger + ensure/stop/retire | being bypassed for coal starts |
| browserctl | one mutation lease per worker; adapter start/stop; watch pane lifecycle | secrets in lease/profile json |

if this split flips, fleets collide on cookies/ports and mirrors freeze.

## the miracle loop (happy path)
1. decide selector: `--profile NAME` OR `--kind scratch|xai|vpn …`
2. `out=$(./bin/browserctl launch … --owner <orch> --json)`
3. read `lease_id` + `env` from out
4. spawn navigator in cwd=`browser-ops` WITH that env (and only that env contract)
5. optional: `./bin/browserctl watch --lease $lease --agent-pane $NAV_PANE --herdr-socket $HERDR_SOCKET_PATH --json`
6. navigator works; daemon updates `state/<worker>/control/active-target.json`; mirror follows target
7. ALWAYS `./bin/browserctl release --lease $lease --json` in finally (closes watch unless `--keep-watch`)

navigator does not pick ports. orchestrator picks profile/lease, then binds navigator.

## env contract (what navigator actually receives)
typical keys from launch:
- `BROWSERCTL_LEASE_ID`
- `BROWSER_HARNESS_WORKER`  (daemon worker id)
- `BROWSER_CDP_URL`         (e.g. http://127.0.0.1:9304)
- `BROWSER_TARGET_STATE`    (active-target.json path)
- `BROWSER_OPS_ROOT` / `BROWSER_OPS_STATE`
- `BROWSER_ALLOW_EVALUATE=1`
- `BROWSERCTL_PROFILE_NAME` (only when launched via `--profile`)
- `PYTHONPATH` → browser-ops

no env, no browser authority.

## three registries (do not collapse them)
1. **PROFILES.json** (tracked): named SELECTORS only. `register / associate / resolve / launch --profile`. no secrets. resolve is exact, never fuzzy, never starts browsers, never auto-creates scratch.
2. **IDENTITIES.json** (runtime, gitignored): xai email ↔ worker ↔ port ↔ profile dir. law: one email ↔ one cloak dir ↔ one worker ↔ one port. active ∩ retired = ∅. ensure on retired needs `--revive`.
3. **API_STAGES.json** (runtime, gitignored): S0–S6 risk ledger for xai api usability (bot flag / billing / reauth budget). S1 flag-alone with APIs 200 = keep. S4 = action_required, NOT auto-teardown. S5/S6 discard+retire.

cloak user-data dirs live under `profiles/xai|scratch|vpn/…` and are runtime-only.

why identities ≠ profiles: profiles are stable human/agent NAMES for launch selectors + site associations. identities are the coal bind plane. stages are health, not naming.

## named profile mechanics
- `profiles register NAME --kind …` maps name → launch selector
- `profiles associate NAME site [account]` ONLY after proven login (never url-inferred)
- `profiles resolve site [--account]` emits launch argv; accountless resolve only matches accountless rows
- `launch --profile NAME` exclusive with selector flags; stamps `lease.profile_name` + `BROWSERCTL_PROFILE_NAME`
- multi-match → PROFILE_AMBIGUOUS; missing → PROFILE_NOT_FOUND

current tracked names:
- `github-ata` → scratch label github-ata; assoc github.com / atacolak
- `scratch-general-1..3` → finite stable scratch pool (no associations yet)

## scratch: ephemeral vs stable
| shape | worker | wipe on release |
|---|---|---|
| ad-hoc `--kind scratch --label X` | unique token | YES if `ephemeral_wipe_v1` |
| `--worker` or named `--profile` scratch | stable | NO |

wipe is strict: marker required, processes dead, paths contained under profiles/scratch + state/. legacy `ephemeral_profile` alone never wipes.

## watch / herdr mirror laws
- watch waits for: cdp `/json/version` + non-null `active_target_id` + id present in `/json/list`
- NEVER seed null active-target (permanent about:blank death)
- viewer: observe_mirror, bounded 1:1 screencast, `VIEWPORT_MODE=fixed` + 1150×902 by default (opt-in `follow-pane`), `VIEWER_WATCH_RESIZE=1`, input read-only
- default split ratio 0.37 → agent left ~37%, browser right ~63%
- exact herdr socket required (fail closed if ambiguous)
- viewer root resolution: HERDR_BROWSER_ROOT → BROWSERCTL_VIEWER_ROOT → state/control/viewer-root
- current viewer-root: `/home/sf/workspace/herdr-browser-cloak`
- unwatch closes mirror; release closes mirror too unless `--keep-watch`

ssh: works when operator is attached to the same herdr session that owns the agent pane + socket. pixels are local-to-herdr, not "a headed chrome on your laptop" unless that path is set up.

## lease laws
- one mutation lease per worker; duplicate → LEASE_CONFLICT
- no managed `default` for leased work (`default` is retired for coal)
- orchestrator owns lease lifecycle; mark-exit if navigator dies without release; reap for ttl
- control plane atomic files under state/control/

## xai / coal specifics
- browserctl launch --kind xai --email → identity_ops ensure
- stop keeps cookies; retire stops + demotes active + may wipe profile
- hard verify after deposit/reauth: live billing 200 + jwt/bot-flag policy per bot-flag-lifecycle skill
- CPA_ADMIN_KEY from env only

## skills / memory
- production procedures: `skills/domains/<site>/…` on main
- interaction patterns: `skills/interactions/`
- quarantine legacy unverified skills: local branch `quarantine/legacy-browser-skills` only — not agent-default memory, not mergeable without revalidation

## live snapshot (2026-07-31, observed)
### leases
1. **active** `e072c0b5e221` worker `scratch-hn-live-demo-dc4e83d0` scratch label hn-live-demo
   - cdp 9304 UP; daemon running; ephemeral_wipe_v1 true; mode persistent; owner orchestrator
   - watch present: agent `w6:p3`, watch `w6:p9`, herdr session `herdr-cloak-experiment`
   - page: hn item about fake authors (news.ycombinator.com)
2. **lease row active but expired=true** `814069c4fa91` worker `scratch-profile-github-ata` profile `github-ata`
   - cdp 9303 UP; stable scratch (no wipe); no watch
   - page: github compare on ogulcancelik/herdr-browser vs atacolak branch
   - needs reap/release hygiene attention (expired but still listed active / daemon up)

### xai identities
all five active binds STOPPED (ports 9223-9227 down):
- hbveapquqs… → xai-hbveapquqs :9223
- omzszspkle… → xai-omzszspkle :9224
- sfwgxhufye… → xai-sfwgxhufye :9225
- sxsuxrnpnd… → xai-sxsuxrnpnd :9226
- pvcvahxqlq… → xai-pvcvahxqlq :9227
retired_identities: 10 historical; unbound_active_auths: 1; missing_shipment_accounts: 0

### other cdp
- 9333 UP — unmanaged `default` worker (NOT for leases). page: getfresh.dev session-persistence docs. treat as leftover lab browser, not fleet capacity.

### code head
recent main commits landed browserctl named profiles, scratch pool, ephemeral_wipe_v1, watch readiness, paired-view resize/reflow, operator doc alignment.

## session arc (2026-07-28 export) — user concerns distilled
do not replay the transcript. these were the recurring questions that built the system:

1. can herdr show multiple headed views for cloak browsers on distinct cdp ports?
2. does that work over ssh attachment to a remote herdr session?
3. who holds the leash — navigator self-provision vs manager/orchestrator lease?
4. want vertical split of the navigator's OWN pane (chat | live browser), same cdp target
5. browserctl must be easy for humans + agents (`--json`), docs lean, not ontology-maxxed
6. verify with multi-navigator demo (x / reddit / hn style concurrency)
7. operator truth too large / noisy — keep only operational insight + cli entrypoints
8. where is navigator cwd/home; should files cluster per browser?
9. dynamic profile capabilities / learn-after-login lookup without overengineered ontology
10. reuse existing ecosystem ideas before inventing cathedral profile metaphysics
11. identities vs API_STAGES vs profiles folder confusion; tracked vs runtime vs quarantine skills
12. pane sizing / mirror usability / what could be upstreamed to herdr-browser

design resolution embodied in main now:
- orchestrator leases; navigator consumes env
- browserctl + watch observe_mirror split
- named profiles = thin selectors + explicit associate
- identities/stages = runtime, not narrative docs
- quarantine branch for unverified skills
- lean AGENTS/README/docs as operator truth

## fleet mental model (how it feels miraculous)
think airport ground control, not "each pilot builds a runway":
- profiles = named aircraft types/routes (selectors)
- identities = tail numbers + gates for coal (hard binds)
- leases = clearance to taxi (mutex)
- daemon+cdp = engine running at a gate
- navigator = pilot who only flies after clearance
- watch pane = tower camera bolted to THAT plane's nose, not a random window
- release = return the gate; ephemeral jets get scrapped, named jets stay hangared

concurrency scales by N leases × N navigators × N mirrors, with collision safety at worker identity, not at chat vibes.

## teaching order (recommended)
1. ownership split + happy path loop
2. live `browserctl list` + one status + active-target
3. scratch ephemeral vs stable vs `--profile`
4. watch mechanics + herdr socket/viewer-root
5. identities + stages (why three files)
6. failure modes: LEASE_CONFLICT, null target blank mirror, expired-but-up daemons, default worker temptation
7. invent a fleet scenario; user predicts commands before seeing them

## open hygiene / gotchas right now
- expired github-ata lease still up on 9303 — teach release/reap discipline
- default:9333 still alive unmanaged — contrast against lease law
- all xai stopped — coal path is cold until ensure/launch
- doc drift risk: some lines still mention older split ratios; AGENTS/watch code prefer 0.37
- html session export is untracked junk in cwd (16mb); not operator truth

## commands cheat sheet
```bash
./bin/browserctl list --json
./bin/browserctl status --lease <id> --json
./bin/browserctl launch --kind scratch --label demo --owner teach --json
./bin/browserctl launch --profile github-ata --owner teach --json
./bin/browserctl watch --lease <id> --agent-pane <pane> --herdr-socket "$HERDR_SOCKET_PATH" --json
./bin/browserctl release --lease <id> --json
./bin/browserctl profiles list --json
./bin/browserctl profiles resolve github.com --account atacolak --json
python3 identity_ops.py list
python3 identity_ops.py stage-list --json
```

## out of scope for this brief
full tool-by-tool navigator grammar, cpa admin deposit runbooks, vpn compose internals, metabolism auto-discard (not shipped), anything from quarantine branch as truth.
