# Browser identity binding

## Rule

```text
1 named face  ↔  1 Cloak profile dir  ↔  1 daemon --worker id  ↔  1 CDP port
1 worker  ↔  1 browser/process lease  ↔  many target leases (one mutating owner each)
```

Cookies **are** the person. Do not share browsers across accounts.

| Map | File |
|---|---|
| stable name → launch + site/account + description | `profiles/PROFILES.json` (**local**, schema **v2**; copy from `PROFILES.example.json`) |
| Cloak user-data dirs | `profiles/scratch/`, `profiles/vpn/` (**runtime**, gitignored) |
| leases | `state/control/leases/` |
| daemon socket | `state/<worker>/daemon.sock` |

---

## CLI

```bash
./bin/browserctl launch --kind scratch --label demo --json
./bin/browserctl release --lease <id> --json

./bin/browserctl profiles register lab-demo --kind scratch --label demo --description 'anon demo' --json
./bin/browserctl profiles associate lab-demo example.com --json   # after proven login
./bin/browserctl profiles resolve example.com --json             # emit only
./bin/browserctl profiles cards --json
./bin/browserctl profiles stamp lab-demo --json
./bin/browserctl launch --profile lab-demo --json   # stamps BROWSERCTL_PROFILE_NAME
```

- Returns `env` (`BROWSER_HARNESS_WORKER`, `BROWSER_TARGET_STATE`, `BROWSERCTL_PROFILE_NAME`, `BROWSER_CDP_URL`, …). `BROWSER_CDP_URL` is for humans/doctor.
- Enforces **one browser/process lease per worker** and **one mutating owner per target**; refuses managed **`default`**.
- `--profile` is exclusive with launch selector flags; associations are explicit (never URL-inferred).
- New named faces require a **description**. `launch.egress` is `direct` or `{type:vpn,…}` — never a peer kind.
- OMP navigators bind via `cloak action=bind` (`omp/cloak.ts`) and drive json-line rpc on the worker daemon socket. They do not shell `browserctl` and do not attach `app.cdp_url`.

Full sheet: [`docs/browserctl.md`](../docs/browserctl.md). Operator law: [`AGENTS.md`](../AGENTS.md). Drive path: [`docs/CONTRACT.md`](../docs/CONTRACT.md).

---

## Scratch / VPN / naming

| Kind | Worker pattern | Profile |
|---|---|---|
| scratch | `scratch-<label>-<token>` (or `scratch-profile-<name>`) | `profiles/scratch/<worker>/` |
| vpn | caller-chosen (not `default`) | `profiles/vpn/<worker>/` |

Ports: scratch 9300–9399 · vpn caller.
**`default` is not a leased worker.**
