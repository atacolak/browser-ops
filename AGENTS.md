# AGENTS.md — browser-ops

Auto-loaded when cwd is `browser-ops`.

---

## What this repo is

Cloak lease plane: tab leases + named profile registry + CDP daemon.

| Path | Role |
|---|---|
| `bin/browserctl` | leases + named profile lookup |
| `browserctl/` | manager, scratch/vpn adapters, `profiles.py` |
| `omp/cloak.ts` | bind + drive + release tool |
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
| agent (`cloak`) | page ops on a leased tab | shell `browserctl`; allocate ports/profiles; attach a second CDP client |
| human / orchestrator | `launch` / `release` / profile register+associate | driving pages; raw daemon ports |

**Rule:** bind once with `cloak action=bind`, then drive with `cloak action=navigate|click|…`. Never attach Puppeteer / a raw CDP URL to a shared port. Never adopt the first/visible tab on a shared browser.

Headed chrome is the profile's `launch.headed` flag. It is a visible Cloak window, nothing else.

---

## Lifecycle

```text
browserctl launch|acquire  →  consume env  →  release
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

1. **One process lease per worker.** Many tab leases may share that browser. At most one writer per tab.
2. Duplicate **process** acquire with exclusive intent → `LEASE_CONFLICT`. Claiming an owned tab without `browserctl --steal` → `TARGET_CONFLICT`. A second `cloak bind` / acquire on the same named profile joins and mints a new tab.
3. **No managed `default`** for leased work.
4. Control plane is atomic files under `state/control/` (no secrets).
5. Harness publishes `state/<worker>/control/active-target.json` (foreground projection). Ownership lives in `targets.json`.
6. Scheduled `browserctl reap` is a **crash backstop** for `one_shot`, `expiring`, or explicit `auto_reap`. Stale tab leases can be reaped without killing a persistent browser that still has other tabs.
7. **Tabs:** `tabs` is a census (`owned_by_me` / `owned_by` / `unowned`). `new_tab` mints a lease. `switch_tab` to a sibling **peeks**. Socket `steal` is `STEAL_FORBIDDEN`. Mutate without an **active** target `lease_id` is `TARGET_LEASE_REQUIRED`. One request, one capability — the daemon does not take a held set. `expiring` occupies; it does not authorize. Before each drive, cloak prunes dead held mappings. Stale bind / `TARGET_LEASE_REQUIRED` best-effort releases every remaining held lease, then drops the sidecar.

```text
browser worker / named profile
├── tab lease A
└── tab lease B
```

### Spawn loop

```text
named profile  →  1 worker / 1 process / 1 CDP
               →  N tab leases (1 writer each)
headed flag    →  visible Cloak window (PROFILES.json launch.headed)
headless       →  same leases, no desktop window
```

| Shape | How |
|---|---|
| 1 profile, 1 client | `cloak bind` named or scratch. one tab. |
| 1 profile, N clients | each `cloak bind` **joins** the worker, mints a new tab. unique leased `targetId` per sidecar |
| 1 profile, headed | `launch.headed: true` on the named face. do **not** pass `--headed` with `--profile` |
| 1 profile, headless | omit `headed` (default). same bind/join |
| ephemeral scratch | `cloak bind` scratch=true → unique worker, `one_shot` |

---

## Skills

| Kind | Where |
|---|---|
| Domain procedure | `skills/domains/<domain>/<skill>.md` |
| Interaction patterns | `skills/interactions/*.md` |
| Named profiles | `profiles/PROFILES.json` via `browserctl profiles …` |
| Spawn / lease law | **this file** — not a skill |

---

## Hard rules

- Do not commit secrets; no secrets in lease/target/PROFILES JSON
- Do not use `default` worker for leased work
- Always `release` when the job is done; `one_shot` + reap is the crash backstop
- CDP on localhost only
- Do not auto-associate profiles from URLs
- Agents: `cloak` only — never shell `browserctl`, never attach raw CDP
