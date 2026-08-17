# Browser identity binding

## Rule

```text
1 email  ↔  1 Cloak profile dir  ↔  1 daemon --worker id  ↔  1 CDP port  ↔  1 navigator env
1 worker  ↔  at most 1 active mutation lease (browserctl)
active ∩ retired  =  ∅
```

Cookies **are** the person. Do not share browsers across accounts.

| Map | File |
|---|---|
| stable name → launch + site/account + description | `profiles/PROFILES.json` (**tracked**, schema **v2**) |
| xAI email ↔ worker ↔ port | `profiles/IDENTITIES.json` (**runtime**, gitignored; bootstrapped empty on first `identity_ops` list/ensure/…) |
| API risk stage S0–S6 | `profiles/API_STAGES.json` (**runtime**, gitignored; bootstrapped empty on first stage-list/get/set) |
| Cloak user-data dirs | `profiles/xai/`, `profiles/scratch/`, `profiles/vpn/` (**runtime**, gitignored) |
| leases | `state/control/leases/` |
| active CDP target | `state/<worker>/control/active-target.json` |

---

## Preferred CLI: browserctl

```bash
./bin/browserctl launch --kind xai --email 'USER@host' --json
./bin/browserctl release --lease <id> --json

./bin/browserctl launch --kind scratch --label demo --json
./bin/browserctl watch --lease <id> --agent-pane <nav-pane> --json
# waits CDP + non-null active_target_id; default agent 37% / browser 63%

# named registry (v2: description required on new register)
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --description 'xAI as USER. Chat only.' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json   # after proven login
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json     # emit only
./bin/browserctl profiles cards --json   # markdown face cards (navigator inject)
./bin/browserctl profiles stamp coal-demo --json   # last_verified_at = now
./bin/browserctl launch --profile coal-demo --json   # stamps BROWSERCTL_PROFILE_NAME
```

- Returns navigator `env` (`BROWSER_HARNESS_WORKER`, `BROWSER_TARGET_STATE`, `BROWSERCTL_PROFILE_NAME`, `BROWSER_CDP_URL`, …).
- Enforces **one mutation lease per worker**; refuses managed **`default`**.
- xAI path calls **`identity_ops`**.
- `--profile` is exclusive with launch selector flags; associations are explicit (never URL-inferred).
- New named faces require a **description**. `launch.egress` is `direct` or `{type:vpn,…}` — never a peer kind. `kind=xai` stays an adapter for old rows; navigators propose `scratch` (+ associate), not `kind=xai`.
- OMP navigators bind via `bind_profile` (session `browser.cdpUrl`). They do not shell `browserctl`.

Full sheet: [`docs/browserctl.md`](../docs/browserctl.md). Operator law: [`AGENTS.md`](../AGENTS.md).

---

## identity_ops (canonical xAI implementation)

```bash
python3 identity_ops.py ensure --email 'USER@host' --json
python3 identity_ops.py ensure --email 'USER@host' --revive --json   # only if retired
python3 identity_ops.py stop --email 'USER@host'
python3 identity_ops.py retire --email 'USER@host' --reason …
python3 identity_ops.py list
python3 identity_ops.py show --email 'USER@host' --json
python3 identity_ops.py stage-get|stage-set|stage-list …
```

| Command | Effect |
|---|---|
| `ensure` | allocate registry row if needed + start daemon; **fails** if email is retired unless `--revive` |
| `stop` | kill daemon+Cloak; **keeps** profile cookies |
| `retire` | stop + remove active row + append `retired_identities` + update stage ledger + wipe profile/state (unless `--keep-profile`) |
| `stage-*` | S0–S6 ledger in `API_STAGES.json` |

**Invariants:** an email cannot be both active and retired. Revive is explicit. Retire updates the stage ledger (`action_required` annotation); it does not call CPA.

Prefer `browserctl launch/release` for leased work; identity_ops for retire/stage/low-level.

---

## Scratch / VPN / naming

| Kind | Worker pattern | Profile |
|---|---|---|
| scratch | `scratch-<label>-<token>` | `profiles/scratch/<worker>/` |
| vpn | caller-chosen (not `default`) | `profiles/vpn/<worker>/` |
| xai | `xai-<slug>` from identity_ops | `profiles/xai/<slug>/` |

Ports: xAI 9223–9299 · scratch 9300–9399 · vpn caller.  
**`default` retired** for coal and browserctl leases.
