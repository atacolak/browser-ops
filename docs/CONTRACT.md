# Drive path

browser-ops leases Cloak processes and tabs. Drive the leased tab through the daemon unix socket. Do not attach a second CDP client to the shared port.

*Inspired by [browser-use/browser-harness](https://github.com/browser-use/browser-harness).*

## Pieces

- `browserctl` — who may use which named face / scratch; one process per worker; many tab leases; one writer per tab
- `daemon/` — holds that worker's CDP connection; json-line rpc on `state/<worker>/daemon.sock`
- `omp/cloak.ts` — bind + drive + release as one tool

## Loop

```
bind {site | profile | scratch}
     → browserctl launch / join
     → Cloak (one process per named face)
     → daemon already holding that chrome
     → sidecar: worker, lease_id, target_id, socket
navigate | click | …
     → json-line on state/<worker>/daemon.sock
     → daemon pins the leased tab, then runs the action
release
     → drop this tab lease (process stays if siblings remain)
```

The client does not pass a CDP URL. Bind pins the tab. Later acts send `{action, target_id, …}` so two clients on one chrome cannot cross-mutate.

## Shared chrome

Two clients, same named profile:

- same worker / same daemon socket / same chrome
- **two** tab leases, two sidecars
- `tabs` lists every page, tagged `owned_by_me` / `owned_by` / `unowned` (lease ids are never published)
- `new_tab` mints a tab lease for the caller and rewrites the sidecar
- `switch_tab` to **your** tab: drive (may bring it forward)
- `switch_tab` to a **sibling** or unowned tab: **peek** (page info + screenshot, no `activateTarget`, sidecar unchanged)
- `close_tab` only on your leases (also releases that lease)
- click/type on a tab you do not hold → `TARGET_CONFLICT`
- socket `steal` → `STEAL_FORBIDDEN`; operator recovery is `browserctl launch --steal --target-id`
- mutating drive without a live **target** `lease_id` → `TARGET_LEASE_REQUIRED` (a process lease is not enough; `--unmanaged` is doctor/debug)
- `LEASE_CONFLICT` only on a second **process** lock with exclusive intent

Ownership is by **lease id**. A lease id is a mutation capability. The model never sees it: `cloak` keeps tokens in the sidecar and strips them from tool text. Bind liveness is cdp + socket + the stored target lease still active and still owning that tab.

## Sidecar

`<session-file>.bind-profile.json`:

```json
{
  "leaseId": "...",
  "targetId": "...",
  "worker": "scratch-profile-lab-demo",
  "socket": "/abs/path/state/<worker>/daemon.sock",
  "cdp": "http://127.0.0.1:93xx",
  "held": { "<targetId>": "<leaseId>" }
}
```

`cdp` is for humans and doctor tools, not a second driver. `held` is every tab this client currently owns. `release` drops all of them.

## `cloak` actions

**lease:** `bind`, `release`

**drive:** `ping`, `page_info`, `navigate`, `screenshot`, `click`, `type`, `press`, `scroll`, `extract`, `fill`, `tabs`, `new_tab`, `switch_tab`, `close_tab`, `dialog`, `wait_for_load`, `wait_for_element`

On the daemon, not on the tool (yet): `evaluate` (gated), `screenshot_base64`, `http_get`, `upload_file`, `run_procedure`, `drain_events`.

`switch_tab` params: `target_id` = destination tab. Peek is the default for tabs you do not hold. Steal is not a socket verb.

## Surfaces

| who | surface |
|---|---|
| human / orchestrator | `./bin/browserctl` (launch, release, profiles, list) |
| agent | `cloak` — bind, then drive; do not shell `browserctl`; do not attach raw CDP |

`cloak` is `hidden` + `discoverable`. A session only sees it if its agent `tools:` list (or `--tools`) names it.

## Not in the public tree

- `profiles/PROFILES.json` (local identities)
- `profiles/scratch/`, `profiles/vpn/` (cookies)
- `skills/domains/**`, `skills/interactions/**`
- `scrapes/`, `findings/`, `state/`, `secrets/`, `.env*`
- bind sidecars, screenshots, daemon logs

Tracked instead: `profiles/PROFILES.example.json`, `profiles/BINDING.md`, tests, this contract.

## Tests that prove the cut

1. two binds on one named scratch profile → two target ids, same worker
2. client A navigate ≠ client B url (daemon pin, not first/visible tab)
3. `cloak` omitted from a session whose agent `tools:` does not list it
4. drive without bind → error, no raw CDP connect
5. `tabs` tags a sibling `owned_by` without leaking their lease id; `switch_tab` peeks; socket `steal` is `STEAL_FORBIDDEN`
6. `new_tab` mints a tab lease; `close_tab` on a sibling is `TARGET_CONFLICT`
7. mutate without `lease_id` → `TARGET_LEASE_REQUIRED`; `--unmanaged` still pins under the drive lock
