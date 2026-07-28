# AGENTS.md — browser-ops

Auto-loaded by pi when cwd is `browser-ops`.  
**Operator truth** for browser agents (especially **navigator**).

---

## What this repo is

Browser automation plane: CloakBrowser + CDP daemon + domain procedure memory + **session leases** + **named profile registry** + **identity binding**.

| Path | Role |
|---|---|
| `bin/browserctl` | session leases + named profile lookup |
| `browserctl/` | lease manager, adapters, `profiles.py` registry |
| `identity_ops.py` | canonical xAI bind/start/stop/retire/stage |
| `daemon/` | CDP harness + active-target publish |
| `vpn/` | geo SOCKS egress helpers |
| `skills/domains/<site>/` | learned site procedures |
| `profiles/PROFILES.json` | **tracked** named selector registry (no secrets) |
| `profiles/IDENTITIES.json` | **runtime** bind map (gitignored; bootstrapped on first use) |
| `profiles/API_STAGES.json` | **runtime** S0–S6 ledger (gitignored; bootstrapped on first use) |
| `profiles/<kind>/` | **runtime** Cloak profile dirs (gitignored) |
| `state/control/` | leases, locks, optional `viewer-root` (no secrets) |

Human entry: [`README.md`](./README.md) · CLI: [`docs/browserctl.md`](./docs/browserctl.md) · Binding: [`profiles/BINDING.md`](./profiles/BINDING.md)

---

## Profiles: who does what

| Profile | Spawn for | Not for |
|---|---|---|
| **navigator** | browser tasks with **already-acquired** env | allocating CDP/ports/profiles; code edits |
| demiurge | code/config changes | driving the browser |
| scout | research / codebase search | browser mutation |
| orchestrator | multi-step dispatch + **browserctl leases** | raw daemon port allocation |

**Rule:** only navigator has browser tools. Other agents route browser work to navigator.  
**Rule:** navigator does **not** allocate raw browser resources. Orchestrator/human runs `browserctl`.

---

## Canonical lifecycle: browserctl

```text
browserctl launch|acquire  →  spawn navigator with returned env  →  release in finally
                 ↘ optional: browserctl watch (observe_mirror pane)
```

```bash
out=$(./bin/browserctl launch --kind scratch --label demo --owner orch --json)
out=$(./bin/browserctl launch --kind xai --email 'USER@host' --owner orch --json)
lease=$(jq -r .lease.lease_id <<<"$out")
# spawn navigator: cwd=browser-ops, env=out.env
./bin/browserctl release --lease "$lease" --json
```

| Kind | Adapter | Notes |
|---|---|---|
| `xai` | `identity_ops ensure/stop` | one email ↔ worker ↔ port; **not** `default` |
| `scratch` / `adhoc` | unique worker + profile + CDP | demos; **explicit** fallback — never auto-created by resolve |
| `vpn` | `vpn/spawn_vpn_browser` | attach existing or start region worker |

### Named profile registry

```bash
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json
```

**Resolve** is exact/deterministic (never fuzzy). Scratch stays explicit; resolve emits selectors only.

### Lease laws

1. **One mutation lease per worker** — duplicate acquire → `LEASE_CONFLICT`.
2. **No managed `default`** for leased work.
3. Control plane is atomic files under `state/control/` (no secrets).
4. Harness publishes `state/<worker>/control/active-target.json` for mirrors.
5. **Orchestrator owns the lease.** Release in `finally`.

---

## xAI / coal (browserctl → identity_ops)

**One account ↔ one Cloak profile ↔ one daemon worker ↔ one CDP port.**  
Runtime files: `profiles/IDENTITIES.json` and `profiles/API_STAGES.json` (bootstrapped empty on first `identity_ops` list/get/set; not committed). Profile dirs under `profiles/` are runtime only.

```bash
./bin/browserctl launch --kind xai --email 'USER@host' --json
python3 identity_ops.py ensure|stop|list|show|retire|stage-get|stage-set …
python3 identity_ops.py ensure --email 'USER@host' --revive   # only if retired
```

**Identity law**

- `active ∩ retired = ∅` (enforced on every registry write)
- `ensure` on retired email **fails** unless `--revive`
- `retire` stops daemon, drops active row, appends `retired_identities`, updates stage ledger, wipes profile unless `--keep-profile`
- paths stored repo-relative under `profiles/` and `state/`

**Hard verify** after every deposit/reauth (not file-on-disk alone):

- access JWT has **no** `bot_flag_source` (or flag alone with billing 200 → S1 keep)
- live bearer billing returns **200**
- management row healthy after a real probe

**Credentials:** `CPA_ADMIN_KEY` from environment only. No admin literals in repo.

Deposit skill: `cpa-manager/xai-dashboard-deposit`. Reauth: `skills/domains/x.ai/reauth.md`.  
Stage canon: `skills/domains/x.ai/bot-flag-lifecycle.md`.

---

## API risk stages (S0–S6) — stand-alone

| Stage | Signal | Action |
|---|---|---|
| **S0** | billing/models 200; flag usually absent | keep in pool |
| **S1** | `bot_flag_source:1` but APIs **200** | **keep** — flag alone ≠ fail |
| **S2** | 403 / bad-credentials on **old** token | **one** full device reauth + hard probe |
| **S3** | 403 / denied **after that fresh** full OAuth | cool-off: max **3** full OAuth/incident, **≥6h** apart; if **48h+** still bad → **S4** |
| **S4** | budget exhausted / still proxy-unusable | **action_required** — orchestrator decides; not automatic delete |
| **S5** | quota-like terminal evidence (operator path) | discard + `identity_ops retire` |
| **S6** | ban/suspend UI | discard + retire |

S3/S4 ≠ S5. Do not loop device reauth past the S3 budget.  
**S4 is never implied-automatic teardown** in this tree.

---

## Memory / skills

| Kind | Where | Discovery |
|---|---|---|
| Domain procedure | `skills/domains/<domain>/<skill>.md` | `browser_skill` list/get/search |
| Interaction patterns | `skills/interactions/*.md` | read when needed |
| Named profiles | `profiles/PROFILES.json` | `browserctl profiles …` |

On learn: prove once → `browser_skill op:write` with frontmatter (`id`, `domain`, `category`, `status`).

---

## What is not in this clean tree

- Destructive live metabolism / auto-discard cron (not shipped)
- Account marketplace scrapers, proxy dump files, therapy datasets
- Payment/SEPA experiments, containment notes, live profiles, findings
- Healing scripts with embedded admin keys

---

## Hard rules

- Do not commit secrets; no secrets in lease/target/PROFILES JSON
- Do not use `default` worker for coal
- Always `release` leases; always close agent-opened tabs
- CDP on localhost only
