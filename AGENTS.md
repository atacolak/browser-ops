# AGENTS.md — browser-ops

Auto-loaded by pi when cwd is `browser-ops`.

---

## What this repo is

Cloak lease plane: session leases + named profile registry + CDP daemon.

| Path | Role |
|---|---|
| `bin/browserctl` | leases + named profile lookup |
| `browserctl/` | manager, scratch/vpn adapters, `profiles.py` |
| `omp/cloak.ts` | OMP navigator `cloak` tool (symlink to `~/.omp/agent/tools/`) |
| `daemon/` | CDP harness + json-line rpc on `state/<worker>/daemon.sock` |
| `vpn/` | geo SOCKS egress helpers |
| `profiles/PROFILES.example.json` | tracked anonymous demo registry |
| `profiles/PROFILES.json` | **local** named faces (gitignored) |
| `profiles/<kind>/` | **runtime** Cloak profile dirs (gitignored) |
| `state/control/` | leases, locks (no secrets) |

Human entry: [`README.md`](./README.md) · CLI: [`docs/browserctl.md`](./docs/browserctl.md) · Binding: [`profiles/BINDING.md`](./profiles/BINDING.md)

---

## Who does what

| Actor | Owns | Must not |
|---|---|---|
| **navigator** | page ops via `cloak` on a leased target | shell `browserctl`; allocate ports/profiles; `xd://browser` |
| orchestrator / human | `launch` / `release` / profile register+associate | driving pages; raw daemon ports |
| demiurge | code/config in this repo | browser mutation |

**Rule:** only navigator has `cloak`. Bind once with `cloak action=bind`, then drive with `cloak action=navigate|click|…`. Never attach `xd://browser` / puppeteer to a CDP port. Never adopt the first/visible tab on a shared browser.

Navigators do **not** invent herdr panes, i3 windows, or viewer daemons. Headed chrome is the profile's launch flag. The pane view is orchestrator work until bind auto-opens a plugin pane (not wired).

---

## Lifecycle

```text
browserctl launch|acquire  →  consume env (BROWSER_CDP_URL)  →  release
```

```bash
out=$(./bin/browserctl launch --kind scratch --label demo --owner orch --mode one_shot --json)
# or: ./bin/browserctl launch --profile github-ata --owner orch --json

lease=$(jq -r .lease.lease_id <<<"$out")
cdp=$(jq -r .env.BROWSER_CDP_URL <<<"$out")

./bin/browserctl release --lease "$lease" --json
```

| Kind | Notes |
|---|---|
| `scratch` / `adhoc` | unique token worker when ad-hoc; **stable** when `--worker` or `--profile` (`scratch-profile-<name>` unless `launch.worker` set). wipe only if `ephemeral_wipe_v1` |
| `vpn` | attach existing or start region worker |

### Named profiles

```bash
./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json
./bin/browserctl profiles resolve example.com --json
./bin/browserctl launch --profile lab-demo --owner orch --json
```

| Rule | Detail |
|---|---|
| Strict selectors | `--profile` **excludes** `--kind/--worker/--label/--cdp-port/--country/--city/--headed/--no-start/--attach-only` |
| Stamp | `--profile` writes `lease.profile_name` + `BROWSERCTL_PROFILE_NAME` |
| Association | **explicit only** after proven login — never infer from the live URL |
| Accountless resolve | without `--account`, only accountless rows match |
| Ambiguity | multi-match → `PROFILE_AMBIGUOUS`; lock timeout → `PROFILE_LOCK_TIMEOUT` |

### Scratch: stable vs ephemeral

| Policy | Use |
|---|---|
| Ephemeral one-shot | `launch --kind scratch --label demo --mode one_shot` — unique worker; release wipes if `ephemeral_wipe_v1` |
| Stable named | `profiles register lab --kind scratch --label lab` + `launch --profile lab` |
| Ports | scratch CDP 9300–9399 under `ports.lock` |

### Lease laws

1. **One browser/process lease per worker.** Many target leases may share that browser. At most one mutating owner per target.
2. Duplicate **browser** acquire with exclusive intent → `LEASE_CONFLICT`. Claiming an owned target → `TARGET_CONFLICT`. A second `cloak bind` / acquire on the same named profile joins and mints a new target.
3. **No managed `default`** for leased work.
4. Control plane is atomic files under `state/control/` (no secrets).
5. Harness publishes `state/<worker>/control/active-target.json` (foreground projection). Ownership lives in `targets.json`.
6. Scheduled `browserctl reap` is a **crash backstop** for `one_shot`, `expiring`, or explicit `auto_reap`. Stale target leases can be reaped without killing a persistent browser that still has other targets.

```text
browser worker / named profile
├── target lease → navigator a
└── target lease → navigator b
```

### Spawn loop (headed / headless / one vs many)

```text
named profile  →  1 worker / 1 process / 1 CDP
               →  N target leases (1 mutating owner each)
headed flag    →  visible cloak window (PROFILES.json launch.headed)
                 NOT a herdr pane, NOT automatic
headless       →  same leases, no desktop window, no pane
```

| Shape | How | Who |
|---|---|---|
| 1 profile, 1 navigator | `cloak bind` named or scratch. one target. | navigator binds; orchestrator only if a pane view is wanted |
| 1 profile, N navigators | each `cloak bind` **joins** the worker, mints a new target. unique leased `targetId` per sidecar | same |
| 1 profile, 1 worker, headed | `launch.headed: true` on the named face. do **not** pass `--headed` with `--profile` | register the face once |
| 1 profile, 1 worker, headless | omit `headed` (default). same bind/join | register or scratch |
| ephemeral scratch | `cloak bind` scratch=true → unique worker, `one_shot` | public pages; wipe if `ephemeral_wipe_v1` |
| persist across yield | hcom named session (`hcom omp --tag …`). in-process `task` navigators die after yield | orchestrator |

Headed **does not** auto-attach a herdr `interactive_mirror` beside the agent. herdr-browser can `plugin pane open` (`placement: split`) but `cloak` does not call it. until that hook exists, the orchestrator splits 33/67, pins a **per-target overlay**, isolates `HERDR_BROWSER_DAEMON_STATE`, `follow-pane`.

hcom `COLLISION … both edited xd://browser` is a **path lock**, not `LEASE_CONFLICT` / `TARGET_CONFLICT`. ignore when the two `BROWSERCTL_TARGET_ID`s differ.

Subagents (omp `task` navigator, hcom peer): same `cloak bind` then `cloak` drive path. they do not get a free pane. persist ≠ pane.

---

## Memory / skills

| Kind | Where |
|---|---|
| Domain procedure | `skills/domains/<domain>/<skill>.md` |
| Interaction patterns | `skills/interactions/*.md` (herdr mirror recipe: `concurrent-targets.md`) |
| Named profiles | `profiles/PROFILES.json` via `browserctl profiles …` |
| Spawn / lease law | **this file** — not a skill |

---

## Hard rules

- Do not commit secrets; no secrets in lease/target/PROFILES JSON
- Do not use `default` worker for leased work
- Always `release` when the job is done; `one_shot` + reap is the crash backstop
- CDP on localhost only
- Do not auto-associate profiles from URLs
- Navigators: `cloak` only — never shell `browserctl`, never `xd://browser`
