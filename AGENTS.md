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
out=$(./bin/browserctl launch --profile coal-demo --owner orch --json)  # named
lease=$(jq -r .lease.lease_id <<<"$out")
# spawn navigator: cwd=browser-ops, env=out.env
# optional mirror (waits CDP + non-null active_target_id; default agent 37% / browser 63%):
# ./bin/browserctl watch --lease "$lease" --agent-pane "$HERDR_PANE_ID" \
#   --herdr-socket "$HERDR_SOCKET_PATH" --json
./bin/browserctl release --lease "$lease" --json   # also closes watch pane unless --keep-watch
```

| Kind | Adapter | Notes |
|---|---|---|
| `xai` | `identity_ops ensure/stop` | one email ↔ worker ↔ port; **not** `default` |
| `scratch` / `adhoc` | unique token worker when ad-hoc; **stable** when `--worker` or `--profile` (named → `scratch-profile-<name>` unless launch.worker set) | demos / finite named pool; wipe only if `ephemeral_wipe_v1` (never legacy `ephemeral_profile` alone) |
| `vpn` | `vpn/spawn_vpn_browser` | attach existing or start region worker |

### Named profile registry

```bash
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json
./bin/browserctl launch --profile coal-demo --owner orch --json
# → lease.profile_name + env.BROWSERCTL_PROFILE_NAME
```

**Resolve** is exact/deterministic (never fuzzy). Scratch stays explicit; resolve emits selectors only.

| Rule | Detail |
|---|---|
| Strict selectors | `--profile` **excludes** `--kind/--email/--worker/--label/--cdp-port/--country/--city/--headed/--no-start/--attach-only` |
| Stamp | successful `--profile` launch writes `lease.profile_name` + `BROWSERCTL_PROFILE_NAME` |
| Association learning | **explicit only** after a proven successful login: `profiles associate <name> <site> [account]` — never infer from the live URL |
| Accountless resolve | without `--account`, only accountless rows match; account-scoped-only site → `PROFILE_NOT_FOUND` (+ hint) |
| Ambiguity | multi-match → `PROFILE_AMBIGUOUS`; lock timeout → `PROFILE_LOCK_TIMEOUT` |

### Scratch: stable vs ephemeral

| Policy | Use |
|---|---|
| Ephemeral one-shot | `launch --kind scratch --label demo --mode one_shot` — unique worker token; release kills daemon/chrome |
| Stable named selector | `profiles register lab-scratch --kind scratch --label lab` + `launch --profile lab-scratch` |
| Ports | scratch CDP 9300–9399 under global `ports.lock` + reservation retry (collision-safe) |

### Watch laws

1. Wait for CDP `/json/version`, non-null `active_target_id`, **and** that id in CDP `/json/list` before splitting (bounded; default 20s).
2. **Never** seed a null active-target stub (that freezes observe_mirror on `about:blank`).
3. Watch pane env must set bounded 1:1 screencast and `HERDR_BROWSER_VIEWER_WATCH_RESIZE=1`. Default viewport is **fixed** 1150×902 (`HERDR_BROWSER_VIEWPORT_MODE=fixed` + width/height) so pane resize scales the frame without reflowing page layout. Opt-in `--viewport follow-pane` sets `VIEWPORT_MODE=follow-pane` and legacy `FOLLOW_PANE_VIEWPORT=1` for dynamic reflow. Input stays read-only in all modes.
4. After pane run, verify viewer process started; on failure close the newly split pane.
5. Default split ratio **0.37** (herdr first-child = agent left 37%, browser right 63%). `--ratio` overrides.
6. Exact herdr endpoint (`--herdr-socket` / `HERDR_SOCKET_PATH`) required — fail closed if ambiguous.
7. Cleanup: `unwatch` closes the mirror pane; `release` closes it too unless `--keep-watch`.

### Lease laws

1. **One mutation lease per worker** — duplicate acquire → `LEASE_CONFLICT`.
2. **No managed `default`** for leased work.
3. Control plane is atomic files under `state/control/` (no secrets).
4. Harness publishes `state/<worker>/control/active-target.json` for mirrors.
5. **Orchestrator owns the lease.** Release in `finally`. Scheduled `browserctl reap` is only a **crash backstop** for auto-reap-eligible leases (`one_shot`, `expiring`, or explicit `auto_reap`) — it does **not** kill default persistent sessions past the 1h TTL stamp. Timer units: `packaging/systemd/user/` + `./bin/browserctl-reap-timer install` (host admin opt-in; do not enable from agent tasks against live shared state).

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

### Quarantine branch (local-only)

Unverified / legacy domain procedures (amazon, facebook, github scraping drafts, etc.) live on the **local** branch `quarantine/legacy-browser-skills` and optional worktree — **not** on `main`, **not** shipped, **not** agent-default memory.

- Do **not** treat quarantine skills as operator truth.
- Do **not** merge quarantine into main without revalidation.
- Edits to quarantine content require a **separate commit on the quarantine branch** (this clean tree does not modify that worktree).
- Revalidation path: prove once against a live lease → rewrite under `skills/domains/<site>/` on main with proper frontmatter.

---

## Hard rules

- Do not commit secrets; no secrets in lease/target/PROFILES JSON
- Do not use `default` worker for coal
- Always `release` leases; always close agent-opened tabs; clean up watch panes
- CDP on localhost only
- Do not open `browserctl watch` before daemon target publish settles; do not seed null targets
- Do not auto-associate profiles from URLs — associate explicitly after proven login
- Do not load skills from the quarantine branch as production procedures
