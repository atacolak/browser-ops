---
id: reauth
domain: x.ai
category: auth
status: active
version: 1
triggers:
  - reauth
  - token expired
  - bad-credentials
  - oauth token invalid
  - heal
  - 403 xai
endpoints:
  - GET  /v0/management/xai-auth-url
  - POST /v0/management/oauth-callback
  - DELETE /v0/management/auth-files?name=<filename.json>
domains:
  - accounts.x.ai
  - auth.x.ai
---

# xAI Account Reauth (Token Refresh)

**Purpose:** Re-authenticate xAI accounts whose OAuth tokens expired (403 bad-credentials).  
**Agent profile:** navigator (or healing script Tier 2 fallback)  
**Sibling skill:** `oauth-deposit.md` — same flow, used for fresh deposits  
**Healing:** manual/orchestrator driven — no auto-heal script in this clean tree

## When to use this

An xAI account returns `HTTP 403 unauthenticated:bad-credentials` / `"The OAuth2 access token could not be validated"`. This means the OAuth refresh token rotted — the account itself is fine, just needs fresh tokens.

**Do NOT reauth if the error is:**
- `account_suspended` / `account_deactivated` / `user_banned` → account is dead, discard it
- `rate_limit_exceeded` → wait and retry, not a reauth issue

## How a navigator should load this

```
browser_skill op:search query:"xai reauth"
browser_skill op:get domain:"x.ai" skill_id:"reauth"
```

Or follow the same flow as `oauth-deposit.md` — the procedure is identical except for Step 0.

## Flow (4 steps)

### Step 0: Delete the rotten auth file

```
DELETE /v0/management/auth-files?name=<auth_id>
Authorization: Bearer <admin_key>
```

This removes the old token so the re-deposit doesn't collide.

### Step 1: Initiate OAuth

```
GET /v0/management/xai-auth-url
Authorization: Bearer <admin_key>
→ { "status": "ok", "url": "https://...", "state": "..." }
```

The OAuth URL contains PKCE challenge, redirect_uri=127.0.0.1:56121/callback, plan=generic.

### Step 2: Browser consent

1. Navigate to the OAuth URL
2. If redirected to sign-in (no active session): click "Login with email" → fill email → next → fill password → sign in
3. Consent page appears ("Authorize Grok Build")
4. **Before clicking Allow**, install the fetch interceptor:

```js
window.__depositLog = [];
var _origFetch = window.fetch;
window.fetch = function(url, opts) {
    window.__depositLog.push({
        url: typeof url === 'string' ? url : (url.url || url.href || ''),
        method: (opts && opts.method) || 'GET'
    });
    return _origFetch.apply(this, arguments);
};
```

5. Click "Allow"
6. Extract code from the intercepted fetch:
   - Pattern: `callback?state=<STATE>&code=<CODE>`
   - Use `browser_act evaluate` to read `window.__depositLog`

**Critical:** The interceptor MUST be installed AFTER reaching the consent page. Page navigation wipes the override.

### Step 3: Complete deposit

```
POST /v0/management/oauth-callback
Authorization: Bearer <admin_key>
Content-Type: application/json
{"state": "<STATE>", "code": "<CODE>"}
→ {"status": "ok"}
```

Verify: `GET /v0/management/auth-files` shows the new auth file with status=active.

## Credentials

Account passwords live only in the external coal shipment secret store:

```text
cpa-farm/secrets/coal-inbox/xai-shipment-*.json
```

Never copy shipment passwords into skills, git, prompts, or browser profile metadata.

## Sign-in patterns (React-controlled inputs)

Use native setter + dispatchEvent for React-controlled inputs:

```js
// Fill email
var i = document.querySelector('input[type="email"]');
var s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
s.call(i, 'email@example.com');
i.dispatchEvent(new Event('input', {bubbles: true}));
i.dispatchEvent(new Event('change', {bubbles: true}));
```

Same pattern for password field. Click buttons by text content:

```js
Array.from(document.querySelectorAll('button'))
  .find(b => b.textContent.includes('Sign in'))
  ?.click();
```

## Multi-account: sign out first

If the browser has session cookies for a previous account, the OAuth URL loads directly to consent. Click "Sign out" button (top-right account menu), then re-navigate to the OAuth URL to trigger sign-in for the next account.

## Anti-bot: fetch interception beats device code

xAI's consent page detects automated browsers and falls back to **device code** instead of HTTP redirect. The fetch interceptor captures the code before the device code fallback renders.

If `window.__depositLog` is empty after clicking Allow, check if:
- The interceptor was installed BEFORE navigation (wiped by page load)
- The consent page rendered device code text instead (visible in `document.body.innerText`)

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| 403 bad-credentials | Token expired | This procedure |
| Account suspended / banned | Account dead | Discard + source new account |
| Sign-in page has CAPTCHA | CloakBrowser fingerprint | Use headed browser on display :1 |
| Interceptor returns empty | Installed too early | Install AFTER consent page loads |
| API returns "state is required" | Wrong POST format | Send `{"state":"...","code":"..."}` not callback URL |
| Device code fallback appears | Bot detection triggered | The fetch interceptor should capture before fallback; if not, read device code text |

## Verification

```bash
# Check auth file is active
curl -sS -H 'Authorization: Bearer $CPA_ADMIN_KEY' http://127.0.0.1:8317/v0/management/auth-files | python3 -c "
import json, sys
for f in json.load(sys.stdin).get('files', []):
    if 'xai' in str(f.get('provider','')).lower():
        print(f\"{f.get('email','?')}: status={f.get('status','?')}\")
"

# Test model availability through the proxy
curl -sS -H 'Authorization: Bearer $CPA_ADMIN_KEY' http://127.0.0.1:8317/v1/models | python3 -c "
import json, sys
for m in json.load(sys.stdin).get('modelSpecs', []):
    if 'grok' in m.get('id','').lower():
        print(m.get('id'))
"
```

## Integration

The orchestrator reads the stage ledger, acquires the bound browser through `browserctl`, and spawns navigator with this skill. No autonomous healer is shipped in this clean tree.
