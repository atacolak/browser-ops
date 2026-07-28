# browser-ops

Local browser control plane: CloakBrowser + CDP daemon + session leases + named profiles + identity binding.

**Agents:** [`AGENTS.md`](./AGENTS.md) · **CLI:** [`docs/browserctl.md`](./docs/browserctl.md) · **Binding:** [`profiles/BINDING.md`](./profiles/BINDING.md)

---

## Quick start — browserctl

```bash
# scratch demo
./bin/browserctl launch --kind scratch --label demo --owner you --json

# xAI coal (requires identity bind)
./bin/browserctl launch --kind xai --email 'USER@host' --json

# named profile registry (no secrets; resolve does not start browsers)
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json

./bin/browserctl watch --lease <id> --agent-pane <nav-pane> --json
./bin/browserctl release --lease <id> --json
./bin/browserctl list --json
```

Spawn navigator **with returned `env`**. It must not allocate ports/profiles.

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
| `profiles/PROFILES.json` | name → launch selector + site/account |
| `profiles/IDENTITIES.example.json` | schema for runtime bind map |
| `state/control/` | leases, locks (runtime, gitignored) |

---

## Identity

```bash
python3 identity_ops.py ensure --email 'USER@host' --json
python3 identity_ops.py stop --email 'USER@host'
python3 identity_ops.py retire --email 'USER@host' --reason …
python3 identity_ops.py stage-get --email 'USER@host' --json
```

- Runtime registry: `profiles/IDENTITIES.json` (gitignored; bootstrapped empty on first use).
- **active ∩ retired = ∅**. `ensure` on a retired email needs `--revive`.
- Admin credentials: **`CPA_ADMIN_KEY` env only** — commands/skills fail closed if absent where needed.

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
- ensure → spawn env → hard verify → stop/release
- No secrets in git, leases, or `PROFILES.json`
- S4 is **action_required** (not automatic teardown)
- CDP localhost only
