---
id: xai-dashboard-deposit
domain: cpa-manager
category: auth
status: active
version: 2
validated: 2026-07-13
validated_accounts: []
version_note: v2.1 — Phase 1 skill-follow green on fresh identity xai-<slug>
triggers:
  - xai
  - deposit
  - oauth
  - start xai login
  - coal
  - grok
  - cpa-manager
  - management.html#/oauth
endpoints:
  - http://127.0.0.1:8316/management.html#/oauth
  - GET /v0/management/auth-files
domains:
  - cpa-manager
  - 127.0.0.1
  - accounts.x.ai
  - auth.x.ai
identity_rule: one xAI account ↔ one Cloak profile (see browser-ops/profiles/IDENTITIES.json)
---

# xAI deposit via CPA Manager Plus dashboard

**Canonical operator path.** Do not free-hand grok.com. Do not prefer raw `curl` `xai-auth-url` as step 1 — the dashboard holds wait/poll state.

**Last green:** (operator-local; not tracked)

**Agent:** navigator · CloakBrowser only · one browser identity per account.

## Load this skill

```
browser_skill op:search query:"xai dashboard deposit"
browser_skill op:get domain:"cpa-manager" skill_id:"xai-dashboard-deposit"
```

Also listed under domain `x.ai` alias skill `oauth-deposit` (pointer). Prefer **this** file.

## Preconditions

| Need | Value |
|---|---|
| Identity | **Orchestrator** ran `python3 identity_ops.py ensure --email <this email> --json` and spawned with emitted `env`. Navigator does not allocate workers. |
| Cloak daemon | worker matching this email in `profiles/IDENTITIES.json` — never `default` for coal |
| CDP | that worker’s port only — never stock Chrome |
| Daemon env | `PYTHONPATH` + `BROWSER_ALLOW_EVALUATE=1` (ensure sets these) |
| Navigator env | `BROWSER_HARNESS_WORKER=<worker_id>` from ensure |
| Step 0 | `browser_admin` status: worker == expected worker_id for this email — else **STOP** |
| CPAMP | `http://127.0.0.1:8316` |
| CPA API | `http://127.0.0.1:8317` · admin key from `CPA_ADMIN_KEY` env (required; fail closed if absent) |
| Password | coal shipment `cpa-farm/secrets/coal-inbox/*.json` |
| Success oracle | `auths/bouncer/xai-<email>.json` exists AND listed in `GET /v0/management/auth-files` |

Identity map: `browser-ops/profiles/IDENTITIES.json`. Binding: `profiles/BINDING.md`.

## Exact flow (do not reorder)

### 1. CPAMP OAuth page

Navigate: `http://127.0.0.1:8316/management.html#/oauth`

If redirected to `#/login`:

1. Fill admin key (native React setter — see below).
2. Check **Remember credential** if present.
3. Click **Login**.
4. Return to `#/oauth` if not already there.

Screenshot before/after login.

### 2. Start xAI Login

Click **Start xAI Login** (or localized `开始 xAI 登录`).

Expect:

- Authorization URL shown (usually `https://accounts.x.ai/oauth2/device?user_code=XXXX-XXXX`)
- **Copy Link** / **Open Link**
- Status: **Waiting for authentication…**

Screenshot.

### 3. Open Link

Click **Open Link** in the UI (keeps CPA polling/state). Prefer this over pasting the URL manually.

Handle new tab: `tabs` / `switch_tab` to the device page.

### 4. Device page

URL shape: `https://accounts.x.ai/oauth2/device?user_code=…`

- If **OneTrust / cookie banner** covers the page, click **Allow All** (or equivalent) once first.
- Code is often prefilled and matches the URL.
- Click **Continue**.

Branches:

| Branch | What you see | Action |
|---|---|---|
| already_session | signed in as **target** email | Continue → consent |
| wrong_session | signed in as **other** email | **Sign out** → restart from Start xAI Login on a clean identity profile |
| need_signin | Sign in to SpaceXAI / Login with email | go to step 5 |

### 5. Sign-in (only if needed)

1. **Login with email**
2. Email field → native setter + `input`/`change` → **Next**
3. Password field → same native setter
4. **Turnstile** (if shown): no reliable DOM handle (iframe/shadow). One careful **visual** click on the empty checkbox left of “Verify you are human” (roughly under the password field, left side). Wait for token; do **not** thrash. If blocked after one careful try → STATUS BLOCKED + screenshot.
5. **Login**

### 6. Authorize Grok Build

Consent copy like: signed in as `<email>` · Authorize Grok Build · permissions list.

**Allow — critical (do not use naive click only):**

React Allow handler is effectively `setAction("allow"); commit()`. A bare `button.click()` often POSTs hidden `action=""` → page **Invalid action** on `auth.x.ai/oauth2/device/approve`.

**Working pattern:**

```js
(() => {
  const form = document.querySelector('form');
  const action = document.querySelector('input[name="action"]');
  if (!form || !action) return 'missing-form';
  action.value = 'allow';
  action.dispatchEvent(new Event('input', { bubbles: true }));
  action.dispatchEvent(new Event('change', { bubbles: true }));
  form.submit();
  return 'submitted-allow';
})()
```

Expect: `https://accounts.x.ai/oauth2/device/done` · **Device Authorized**.

If you hit **Invalid action**: do not loop Allow. Return to CPAMP, **Start xAI Login** again (fresh code), Open Link, Continue, then form.submit with `action=allow`.

### 7. CPA success

Back on `#/oauth`: waiting/polling clears · success / idle (no stuck Waiting banner).

### 8. Verify (required)

```bash
ls "${CPA_AUTH_DIR:-$HOME/cpa-farm/auths/bouncer}/xai-<email>.json"
curl -sS -H 'Authorization: Bearer <admin>' http://127.0.0.1:8317/v0/management/auth-files
```

Deposit is successful when the **file exists** and the email is listed.  
`status=error` / chat permission-denied after a fresh deposit may be product entitlement noise — do not immediately reauth unless it persists or operator asks.

## React input fill (email/password/admin key)

```js
(function(sel, value){
  var i = document.querySelector(sel);
  if (!i) return 'missing';
  var s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  s.call(i, value);
  i.dispatchEvent(new Event('input', { bubbles: true }));
  i.dispatchEvent(new Event('change', { bubbles: true }));
  return 'ok';
})('input[type="email"]', 'user@example.com')
```

## Identity / profile rules

- **One account ↔ one Cloak `user-data-dir` ↔ one daemon worker ↔ one CDP port.**
- Do not deposit account B in a profile that still has account A’s xAI cookies unless you fully Sign out (prefer separate profile).
- Parallel deposits require **N daemons**, not one port with many profiles.

## Failure → fix table (self-heal seeds)

| Symptom | Likely cause | Fix |
|---|---|---|
| Invalid action after Allow | empty hidden `action` | set `action=allow` + `form.submit()` |
| Turnstile blocks Login | bot wall | one coordinate click on checkbox; else BLOCKED for human |
| CPA stuck Waiting after failed Allow | dead device code | reload `#/oauth`, Start xAI Login (new code) |
| Wrong user on consent | shared profile cookies | Sign out or switch to bound profile |
| Open Link does nothing | popup/tab | check tabs; navigate URL from CPA field as fallback and note it |
| No auth file after Device Authorized | CPA poll lag / wrong bouncer | re-check API/disk after ~5–15s; confirm CPAMP points at bouncer |

On heal: screenshot + DOM snippet → apply fix → if durable, `browser_skill op:write` to update this skill (with operator approval in Phase 1).

## Out of scope

- Metabolism auto-discard (cron)
- Multi-account parallel (separate workers)
- Deterministic YAML promotion (only after N green skill-following runs)
- Raw API-first PKCE path (legacy; see historical notes in older `x.ai/oauth-deposit` revisions)

## Shipment

`cpa-farm/secrets/coal-inbox/xai-shipment-*.json` — email/password pairs for disposable coal only.
