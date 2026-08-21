# browser-ops

Inspired by [browser-use/browser-harness](https://github.com/browser-use/browser-harness) — a thin browser-driving loop, with optional markdown skills as operator notes. This repo does **not** vendor that runtime, Python helpers, or skill files. Leases, Cloak, and the daemon rpc here are original.

A local **lease plane + drive daemon** for Cloak: one browser process per worker, many target leases, one mutating owner per target. Named faces and anonymous scratch live in a local registry; omp navigators drive a leased tab only through the daemon unix socket.

**CLI:** [`docs/browserctl.md`](./docs/browserctl.md) · **Binding:** [`profiles/BINDING.md`](./profiles/BINDING.md) · **Topology:** [`docs/topology-brief.md`](./docs/topology-brief.md)

---

## Lease laws

- One browser/process lease per worker. Many target leases may share that browser.
- One mutating owner per target. Claiming an owned target → `TARGET_CONFLICT`.
- Exclusive second browser lease → `LEASE_CONFLICT`. A compatible second bind **joins** and mints a new target.
- No managed `default` worker for leased work.
- No secrets in git, leases, or profile JSON.
- CDP on localhost only.
- Associate a named face only after proven login. Resolve never starts browsers or invents scratch.

```text
browser worker / named profile
├── target lease → navigator a
└── target lease → navigator b
```

## Daemon socket

`daemon/` holds the worker's CDP connection. Rpc is one json line on `state/<worker>/daemon.sock`:

```json
{ "action": "navigate", "target_id": "<optional>", "url": "https://example.com" }
```

If `target_id` is present, the daemon pins that CDP session under an `asyncio.Lock` and then runs the action. If omitted, the current session is used (doctor / legacy). Navigators never attach puppeteer, `xd://browser`, or a raw `cdp_url` to the shared port.

## omp `cloak` tool

One custom tool named `cloak` (`hidden: true`). Source: `omp/cloak.ts` (symlink into `~/.omp/agent/tools/`). Navigators list `tools: cloak, read, grep, glob, bash, write`. Coding sessions do not see `cloak` unless they list it.

| action | role |
|---|---|
| `bind` / `release` | lease a named face, scratch, or site; drop this target only |
| `navigate`, `click`, `type`, … | json-line to the worker daemon socket |

Bind writes a sidecar `<session>.bind-profile.json` (`leaseId`, `targetId`, `worker`, `socket`, `cdp`). `cdp` is for humans/doctor. `cloak` never prints `app.cdp_url`. Drive after bind goes to the socket, not to a browser-open URL.

---

## Quick start (CLI)

Humans and orchestrators use `./bin/browserctl`. Navigators use `cloak` only — they do not shell this CLI.

```bash
# env-contract (lease id + worker + cdp for doctor)
./bin/browserctl launch --kind scratch --label demo --owner you --json
./bin/browserctl release --lease <id> --json

# named face from a local registry (copy the example first)
cp profiles/PROFILES.example.json profiles/PROFILES.json
./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json
./bin/browserctl profiles resolve example.com --json
./bin/browserctl launch --profile lab-demo --owner you --json

./bin/browserctl list --json

# finite job: one_shot is auto-reap eligible if the process dies
./bin/browserctl launch --kind scratch --label demo --mode one_shot --owner orch --json
# host admin (opt-in crash backstop): ./bin/browserctl-reap-timer install --enable --now
```

---

## Layout

| Path | Purpose |
|---|---|
| `bin/browserctl` | leases + `profiles` CLI |
| `browserctl/` | manager, scratch/vpn adapters, registry |
| `omp/cloak.ts` | OMP `cloak` tool (symlink into `~/.omp/agent/tools/`) |
| `daemon/` | CDP harness + json-line rpc on `state/<worker>/daemon.sock` |
| `vpn/` | gluetun compose + VPN browser spawn |
| `profiles/PROFILES.example.json` | **tracked** anonymous demo registry (copy to `PROFILES.json`) |
| `profiles/PROFILES.json` | **local** named selector registry (gitignored) |
| `profiles/scratch/`, `profiles/vpn/` | **runtime** Cloak user-data (gitignored) |
| `state/control/` | leases, locks (runtime, gitignored) |

---

## Env contract

`BROWSERCTL_LEASE_ID` · `BROWSERCTL_TARGET_ID` · `BROWSERCTL_BROWSER_LEASE_ID` · `BROWSER_HARNESS_WORKER` · `BROWSER_CDP_URL` · `BROWSER_TARGET_STATE` · `BROWSER_OPS_ROOT` · `BROWSER_ALLOW_EVALUATE`

Root discovery: `--root` → `BROWSER_OPS_ROOT` → this checkout. Ad-hoc debug: `./bin/start-daemon …` — not for leases. `BROWSER_CDP_URL` is for humans/doctor, not for navigator attach.

---

## Tests

```bash
python3 -m pytest tests/ -q
```
