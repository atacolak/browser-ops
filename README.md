# browser-ops

Local browser control plane: CloakBrowser + CDP daemon + session leases + named profiles + identity binding.

**Agents:** [`AGENTS.md`](./AGENTS.md) · **CLI:** [`docs/browserctl.md`](./docs/browserctl.md) · **Binding:** [`profiles/BINDING.md`](./profiles/BINDING.md)

---

## Quick start — browserctl

```bash
# orchestrator navigator lifecycle (preferred)
./bin/browserctl navigator spawn --kind scratch --label demo --owner you --json
./bin/browserctl navigator cleanup --lease <id> --json

# env-contract only (no pane)
./bin/browserctl launch --kind scratch --label demo --owner you --json

# xAI coal (requires identity bind)
./bin/browserctl navigator spawn --kind xai --email 'USER@host' --json

# named profile registry (no secrets; resolve does not start browsers)
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json

./bin/browserctl list --json

# finite navigator job: one_shot is auto-reap eligible if process crashes
./bin/browserctl spawn --kind scratch --label demo --mode one_shot --owner orch --json
# fresh orchestrators use navigator cleanup in finally; the timer is crash backstop only
# host admin (opt-in): ./bin/browserctl-reap-timer install --enable --now
```

Navigators are orchestrator-only. Prefer `navigator spawn` / `navigator cleanup` over hand-joining lease + agent-ctl + watch + release. Navigators must not allocate ports/profiles.

---

## Layout

| Path | Purpose |
|---|---|
| `bin/browserctl` | leases + `profiles` lookup CLI |
| `browserctl/` | manager, adapters, registry, watch |
| `identity_ops.py` | xAI ensure/stop/retire/stage |
| `daemon/` | CDP harness + active-target publish |
| `vpn/` | gluetun compose + VPN browser spawn |
| `skills/domains/` | curated site procedures (xAI, fines, …) |
| `profiles/PROFILES.json` | **tracked** named selector registry (no secrets) |
| `profiles/IDENTITIES.json` | **runtime** bind map (gitignored; bootstrapped on first use) |
| `profiles/API_STAGES.json` | **runtime** S0–S6 ledger (gitignored; bootstrapped on first use) |
| `profiles/xai/`, `scratch/`, … | **runtime** Cloak profile dirs (gitignored) |
| `state/control/` | leases, locks, optional `viewer-root` (runtime, gitignored) |

---

## Identity

```bash
python3 identity_ops.py ensure --email 'USER@host' --json
python3 identity_ops.py stop --email 'USER@host'
python3 identity_ops.py retire --email 'USER@host' --reason …
python3 identity_ops.py stage-get --email 'USER@host' --json
```

- `profiles/IDENTITIES.json` and `profiles/API_STAGES.json` are gitignored runtime files; `identity_ops` bootstraps each empty on first list/get/set.
- Profile dirs under `profiles/` are runtime only — never commit live data.
- **active ∩ retired = ∅**. `ensure` on a retired email needs `--revive`.
- Admin credentials: **`CPA_ADMIN_KEY` env only** — commands/skills fail closed if absent where needed.

### Watch / observe_mirror

`browserctl watch` waits for CDP + non-null `active_target_id` present in `/json/list` (no null stub / permanent `about:blank`), uses bounded 1:1 screencast with default **fixed** layout viewport 1150×902 (pane resize scales the frame; page layout stays stable), verifies the viewer process started (closes the new pane on failure), then keeps the split. Opt-in `--viewport follow-pane` restores dynamic page reflow. Default ratio **agent 37% / browser 63%** (`--ratio` overrides). `release`/`unwatch` clean up the pane.

Viewer root (fail closed), first capable match:

1. `HERDR_BROWSER_ROOT`
2. `BROWSERCTL_VIEWER_ROOT`
3. repo-local `state/control/viewer-root` (single-line path; gitignored under `state/`)

```bash
# one-time per checkout (path is local; not committed)
mkdir -p state/control
echo '/path/to/observe_mirror-capable/herdr-browser' > state/control/viewer-root
```

Full contract: [`docs/browserctl.md`](./docs/browserctl.md) · agent law: [`AGENTS.md`](./AGENTS.md).

### Quarantine (local-only)

Legacy unverified domain skills sit on local branch `quarantine/legacy-browser-skills` only — not main, not default agent memory. See AGENTS.md.

---

## Daemon env (from browserctl)

`BROWSER_HARNESS_WORKER` · `BROWSER_OPS_ROOT` · `BROWSER_ALLOW_EVALUATE` · `BROWSERCTL_LEASE_ID` · `BROWSER_TARGET_STATE`

Ad-hoc debug: `./bin/start-daemon …` — not for coal/leases.

---

## Tests

```bash
python3 -m pytest tests/ -q
```

---

## Hard rules (short)

- Navigator is the only browser actor; orchestrator owns browserctl lifecycle
- One identity/profile/worker/port; no leased `default`
- ensure → spawn env → hard verify → stop/release (and unwatch)
- Named profiles: register / associate / resolve / `launch --profile` (strict selectors; stamp `BROWSERCTL_PROFILE_NAME`)
- Associate only after proven login; resolve never auto-creates scratch
- Watch waits for daemon target publish + `/json/list` match; `VIEWER_WATCH_RESIZE=1`; default split 25/75
- No secrets in git, leases, or `PROFILES.json`
- S4 is **action_required** (not automatic teardown)
- CDP localhost only
