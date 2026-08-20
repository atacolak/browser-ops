# browser-ops

Local Cloak lease plane: one browser/process per worker, many target leases, named profiles, scratch or vpn.

**Agents:** [`AGENTS.md`](./AGENTS.md) · **CLI:** [`docs/browserctl.md`](./docs/browserctl.md) · **Binding:** [`profiles/BINDING.md`](./profiles/BINDING.md)

---

## Quick start

```bash
# env-contract (cdp_url + lease id)
./bin/browserctl launch --kind scratch --label demo --owner you --json
./bin/browserctl release --lease <id> --json

# named face (no secrets; resolve does not start browsers)
./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json
./bin/browserctl profiles resolve example.com --json
./bin/browserctl launch --profile lab-demo --owner you --json

./bin/browserctl list --json

# finite job: one_shot is auto-reap eligible if the process dies
./bin/browserctl launch --kind scratch --label demo --mode one_shot --owner orch --json
# host admin (opt-in crash backstop): ./bin/browserctl-reap-timer install --enable --now
```

OMP navigators bind with `bind_profile`, then `browser` open `app.cdp_url` **and** `app.target_id` (the leased target). They do not shell `browserctl` and must not adopt the first or visible tab.

---

## Layout

| Path | Purpose |
|---|---|
| `bin/browserctl` | leases + `profiles` CLI |
| `browserctl/` | manager, scratch/vpn adapters, registry |
| `omp/bind-profile.ts` | OMP `bind_profile` tool (symlink into `~/.omp/agent/tools/`) |
| `daemon/` | CDP harness + active-target publish |
| `vpn/` | gluetun compose + VPN browser spawn |
| `skills/domains/` | site procedures |
| `profiles/PROFILES.json` | **tracked** named selector registry (no secrets) |
| `profiles/scratch/`, `profiles/vpn/` | **runtime** Cloak user-data (gitignored) |
| `state/control/` | leases, locks (runtime, gitignored) |

---

## Env contract

`BROWSERCTL_LEASE_ID` · `BROWSERCTL_TARGET_ID` · `BROWSERCTL_BROWSER_LEASE_ID` · `BROWSER_HARNESS_WORKER` · `BROWSER_CDP_URL` · `BROWSER_TARGET_STATE` · `BROWSER_OPS_ROOT` · `BROWSER_ALLOW_EVALUATE`

Root discovery: `--root` → `BROWSER_OPS_ROOT` → this checkout. Ad-hoc debug: `./bin/start-daemon …` — not for leases.

---

## Tests

```bash
python3 -m pytest tests/ -q
```

---

## Hard rules

- One browser/process lease per worker; many target leases; one mutating owner per target; no leased `default`
- Named profiles: register / associate / resolve / `launch --profile`
- Associate only after proven login; resolve never starts browsers or invents scratch
- No secrets in git, leases, or `PROFILES.json`
- CDP localhost only
