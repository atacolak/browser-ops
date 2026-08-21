# browser-ops

*Inspired by [browser-use/browser-harness](https://github.com/browser-use/browser-harness).*

Lease a [Cloak](https://github.com/CloakLabs/cloakbrowser) browser, then drive a specific tab over a unix socket. One Chrome process per named face. Many tabs can share that process. Each tab has at most one writer.

You do not attach Puppeteer or a raw CDP URL to a shared port. The daemon already holds that connection. You send one JSON line per action.

## What it can do

- **Scratch browsers** — throwaway or stable anonymous profiles, CDP ports 9300–9399
- **Named faces** — register a profile, associate it with a site after login, resolve it later without guessing
- **VPN faces** — attach or start a region worker (`--kind vpn`)
- **Headed or headless** — `launch.headed` on the named face; same lease model either way
- **Page ops** — navigate, click, type, fill, press, scroll, screenshot, extract, dialogs, wait for load/element
- **Shared chrome** — two clients on one profile get two tabs. `tabs` tags each page `owned_by_me` / `owned_by` / `unowned`. `new_tab` mints a lease. `switch_tab` to someone else's tab **peeks** (page info + screenshot, window stays put). `close_tab` only closes tabs you own. Steal is operator recovery (`browserctl --steal`), not an agent verb.
- **Crash backstop** — `one_shot` leases expire; `browserctl reap` cleans them without killing a persistent browser that still has other tabs

CDP stays on localhost. Cookies live in gitignored profile dirs. No secrets in lease JSON.

## How the pieces fit

```
you
  ├─ CLI  ./bin/browserctl launch|release|profiles …
  └─ tool omp/cloak.ts   bind → drive → release
           │
           ▼
     browserctl          mutex + named-profile registry
           │             one process lease per worker
           │             many tab leases on that process
           ▼
     daemon.sock         json-line rpc
           │             pins the leased tab, then runs the action
           ▼
     Cloak / Chromium    one user-data dir, one CDP port
```

`launch` starts (or joins) the browser, mints a **tab lease**, and prints env. The daemon is already listening on `state/<worker>/daemon.sock`. Drive commands are `{ "action": "navigate", "target_id": "…", "url": "…" }`. `release` drops that tab; the process stays up if other tabs are still leased.

```text
named profile / worker
├── tab lease A   (you)
└── tab lease B   (a sibling client)
```

## Quick start

```bash
# anonymous scratch
./bin/browserctl launch --kind scratch --label demo --owner you --json
./bin/browserctl release --lease <id> --json

# named face (copy the example registry first)
cp profiles/PROFILES.example.json profiles/PROFILES.json
./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json   # after a real login
./bin/browserctl profiles resolve example.com --json
./bin/browserctl launch --profile lab-demo --owner you --json

./bin/browserctl list --json

# finite job: auto-reap if the process dies
./bin/browserctl launch --kind scratch --label demo --mode one_shot --owner you --json
```

From an agent, `cloak` is the same loop without shelling the CLI:

```
bind {profile | site | scratch:true}
navigate / click / type / tabs / …
release
```

## Tab sharing

Same profile, two clients:

| action | effect |
|---|---|
| `tabs` | census of every page, tagged by ownership (lease ids stay in the sidecar) |
| `new_tab` | create a tab **and** a lease for you |
| `switch_tab` to your tab | send that tab's lease; drive it (brings it forward) |
| `switch_tab` to a sibling | peek — no `activateTarget`, your pin stays |
| `close_tab` | only with that tab's lease; also releases it |
| click/type on a tab this lease doesn't own | `TARGET_CONFLICT` |
| socket `steal` | `STEAL_FORBIDDEN` — recovery is `browserctl launch --steal --target-id` |

Mutating drive needs an **active** target `lease_id` that owns that tab. Occupancy (`expiring`) is not authority. A process lease cannot mint or click. `--unmanaged` on the daemon is doctor/debug only. `cloak` never prints lease ids to the model. The daemon does not take a held-lease set. Before each drive, cloak verifies every held lease and drops stolen siblings from the sidecar. Stale bind and `TARGET_LEASE_REQUIRED` release every remaining held lease, then drop the sidecar.

A second **browser** lock on the same worker with exclusive intent is `LEASE_CONFLICT`. A compatible second `launch` / `bind` **joins** and mints a new tab.

## Layout

| Path | Purpose |
|---|---|
| `bin/browserctl` | leases + profile CLI |
| `browserctl/` | manager, scratch/vpn adapters, registry |
| `daemon/` | holds CDP; json-line rpc on `state/<worker>/daemon.sock` |
| `omp/cloak.ts` | bind + drive + release as one tool |
| `vpn/` | region SOCKS / gluetun helpers |
| `profiles/PROFILES.example.json` | tracked demo registry — copy to `PROFILES.json` |
| `profiles/PROFILES.json` | **local** named faces (gitignored) |
| `profiles/scratch/`, `profiles/vpn/` | **runtime** user-data (gitignored) |
| `state/control/` | leases and locks (runtime, gitignored) |

## Env (from `launch --json`)

| Key | Meaning |
|---|---|
| `BROWSERCTL_LEASE_ID` | tab lease — this is what you `release` |
| `BROWSERCTL_TARGET_ID` | owned CDP page |
| `BROWSERCTL_BROWSER_LEASE_ID` | process lease |
| `BROWSER_HARNESS_WORKER` | worker id / socket dirname |
| `BROWSER_CDP_URL` | `http://127.0.0.1:93xx` — inspect, don't attach a second driver |
| `BROWSER_TARGET_STATE` | `state/<worker>/control/active-target.json` |
| `BROWSERCTL_PROFILE_NAME` | set only when launched with `--profile` |

Root: `--root` → `BROWSER_OPS_ROOT` → this checkout.

## Docs

- CLI sheet: [`docs/browserctl.md`](./docs/browserctl.md)
- Drive path: [`docs/CONTRACT.md`](./docs/CONTRACT.md)
- Identity: [`profiles/BINDING.md`](./profiles/BINDING.md)
- Topology: [`docs/topology-brief.md`](./docs/topology-brief.md)

## Tests

```bash
python3 -m pytest tests/ -q
```
