# Drive path (law)

browser-ops leases Cloak processes and target tabs. omp navigators drive the leased tab **only** through the daemon unix socket. they never attach puppeteer / `xd://browser` to the shared CDP port.

## What this repo is

A local **lease plane + drive daemon** for Cloak:

- `browserctl` — who may use which named face / scratch; one browser process per worker; many target leases; one mutating owner per target.
- `daemon/` — holds that worker's CDP connection; json-line rpc on `state/<worker>/daemon.sock`.
- `omp/cloak.ts` — the only omp-facing agent api (custom tool).

Inspired by [browser-use/browser-harness](https://github.com/browser-use/browser-harness) (markdown skills, thin CDP hands). **runtime, leases, Cloak, and this rpc are original.** we do not vendor their python, `agent_helpers.py`, or interaction/domain skill files.

## OMP tools: there is no pack type

omp `agents/<name>.md` frontmatter `tools:` is an **allowlist of tool names**. there is no group/pack/extension that expands into many names.

so the "pack" is **one tool** named `cloak` whose `action` enum is bind + page ops.

| session | how `cloak` appears |
|---|---|
| parent / coding agent | `hidden: true` — **not** in the model tool list unless `--tools cloak` or an agent lists it |
| `agents/navigator.md` | `tools: cloak, read, grep, glob, bash, write` (omp auto-adds `yield` / `hub`) |

custom tools load from `~/.omp/agent/tools/*.ts` (symlink to `omp/cloak.ts`). a factory **may** return an array of tools from one file; each still has its own name and must be listed separately. we do **not** do that here — one name.

`xd://browser` stays for coding agents (localhost UIs, relay). navigators do **not** get `browser`.

mcp is a later stdio face of the same daemon rpc. not this cut.

## Loop

```
navigator
  cloak action=bind {site|profile|scratch}
       → browserctl launch/join
       → cloak chrome (one process / named face)
       → daemon.main already holding that chrome
       → sidecar: worker, lease_id, target_id, socket
  cloak action=navigate|click|…   (no target_id)
       → json-line to state/<worker>/daemon.sock
       → daemon session pinned to leased target
  cloak action=release
       → drop this target lease (browser stays if siblings exist)
```

the model does **not** pass `cdp_url` or `target_id` on every click. bind pins the session. first drive after bind `switch_tab`s to the leased id if needed; later acts use the daemon's current session.

## Swarm

two navigators, same named profile:

- same worker / same daemon socket / same chrome
- **two** target leases, two sidecars (per omp session file)
- neither `cloak` act may switch to the other's target
- `TARGET_CONFLICT` if someone claims an owned target
- `LEASE_CONFLICT` only on a second **browser** exclusive acquire

## Sidecar (not for the model)

`<session-file>.bind-profile.json` (keep filename for now):

```json
{
  "leaseId": "...",
  "targetId": "...",
  "worker": "scratch-profile-lab-demo",
  "socket": "/abs/path/state/<worker>/daemon.sock",
  "cdp": "http://127.0.0.1:93xx"
}
```

`cdp` is for humans/doctor, **not** for `xd://browser`. `cloak` never prints "open with app.cdp_url".

## `cloak` actions

**lease:** `bind`, `release`

**drive (daemon rpc):** `ping`, `page_info`, `navigate`, `screenshot`, `click`, `type`, `press`, `scroll`, `extract`, `fill`, `tabs`, `new_tab`, `switch_tab`, `close_tab`, `dialog`, `wait_for_load`, `wait_for_element`

omit from v1 (keep on daemon, not on the tool): `evaluate` unless gated, `screenshot_base64`, `http_get`, `upload_file`, `run_procedure`, `drain_events`. add later.

`new_tab` / `close_tab` / `switch_tab` on the tool must not steal a sibling's leased target. bind-minted id is the only default. `switch_tab` to an unleased id is an error.

## CLI vs agent

| actor | surface |
|---|---|
| human / orchestrator | `./bin/browserctl` (launch, release, profiles, list) |
| navigator | `cloak` only — never shell `browserctl`, never raw CDP |
| coding agent | no `cloak` (hidden); may use `xd://browser` for non-cloak jobs |

## Not in the public git tree

- `profiles/PROFILES.json` (operator identities: github-ata, ubereats, …)
- `profiles/scratch/`, `profiles/vpn/` (cookies)
- `skills/domains/**` (operator site procedures)
- `skills/interactions/*` copied from browser-use (connection, cookies, tabs stubs, profile-sync, …)
- `scrapes/`, `findings/`, `state/`, `secrets/`, `.env*`
- bind sidecars, screenshots, daemon logs

tracked instead: `profiles/PROFILES.example.json`, `profiles/BINDING.md`, tests, this contract.

## Tests that prove the cut

1. two `cloak bind` on one named scratch profile → two target ids, same worker.
2. navigator A navigate ≠ navigator B url (daemon pin, not puppeteer first/visible).
3. `cloak` omitted from a session whose agent `tools:` does not list it.
4. `cloak` act without bind → error, no raw cdp connect.

mcp, herdr panes, and omp-alt `app.target_id` patches are **out of scope**.
