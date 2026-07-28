---
id: reddit-scraping
domain: reddit
category: scraping
status: active
---

# Reddit — read-only post extraction

Read-only. No voting, commenting, posting, or account mutation.

New Reddit (`www.reddit.com`) is a Lit/web-components SPA (`shreddit-post`, `shreddit-comment`, `faceplate-*`). Custom element tags are relatively stable versus hashed class names.

**Prefer JSON (`.json` / public endpoints) via `browser_act` `http_get` for public posts.** Use the browser harness when logged-in session, NSFW gates, private/quarantined subs, or anti-bot 429s require it.

## URL patterns

| Shape | Notes |
|---|---|
| `https://www.reddit.com/r/<sub>/comments/<id>/<slug>/` | full post |
| `https://www.reddit.com/r/<sub>/s/<hash>` | share short-link; resolves after load |
| `https://www.reddit.com/r/<sub>/comments/<id>/.json` | anonymous JSON for public posts |
| `https://old.reddit.com/r/<sub>/comments/<id>/` | simpler DOM fallback (no `shreddit-*`) |

## Path 1: JSON API (public)

```text
browser_act action=http_get
  url=https://www.reddit.com/r/<sub>/comments/<id>/.json
```

Use a normal browser-like `User-Agent` header when the daemon accepts headers. Parse JSON:

```python
import json
data = json.loads(body)
post = data[0]["data"]["children"][0]["data"]
# title, selftext, author, score, num_comments, created_utc, url, permalink
comments = data[1]["data"]["children"]  # t1 nodes or kind "more"
```

Fails on private/quarantined (401), NSFW without session, or 429 under load — back off or switch to browser path.

## Path 2: Browser DOM (session / gates)

```text
browser_act action=new_tab url=https://www.reddit.com/r/<sub>/comments/<id>/
# or navigate on an existing tab
browser_act action=wait_for_load
# SPA often needs a short extra wait after readyState
browser_act action=scroll dy=2000
browser_act action=scroll dy=2000
browser_act action=evaluate expression=<JS below>
```

Map legacy names: `new_tab` / `wait_for_load` / `scroll` / `evaluate` are `browser_act` actions — not bare JS helpers, legacy goto wrappers, or shell harness here-docs.

### Extraction JS (pass to evaluate; stringify structured returns)

```js
JSON.stringify((() => {
  const postEl = document.querySelector("shreddit-post");
  if (!postEl) return null;
  const title =
    (postEl.querySelector('h1, [slot="title"]') || {}).innerText?.trim() || "";
  const bodyEl = postEl.querySelector('[slot="text-body"] .md, [slot="text-body"]');
  const body = bodyEl ? bodyEl.innerText.trim() : "";
  const author =
    (postEl.querySelector('[slot="authorName"] a, a[data-testid="post_author_link"]') || {})
      .innerText?.trim() || "";
  const subM = location.pathname.match(/^\/r\/([^/]+)/);
  const scoreEl = postEl.querySelector("faceplate-number");
  const score = scoreEl
    ? scoreEl.getAttribute("number") || scoreEl.innerText
    : "";
  const comments = [];
  for (const c of document.querySelectorAll('shreddit-comment[depth="0"]')) {
    const cBodyEl = c.querySelector('[slot="comment"] .md, [slot="comment"]');
    const cBody = cBodyEl ? cBodyEl.innerText.trim() : "";
    if (!cBody) continue;
    comments.push({
      author: c.getAttribute("author") || "",
      score: c.getAttribute("score") || "",
      body: cBody,
    });
    if (comments.length >= 10) break;
  }
  return {
    subreddit: subM ? subM[1] : "",
    title,
    author,
    score,
    body,
    comments,
    url: location.href,
  };
})())
```

### Key selectors

| Target | Selector |
|---|---|
| Post | `shreddit-post` (attrs: `post-title`, `post-id`, `subreddit-name`, `author`) |
| Title | `shreddit-post h1` or `[slot="title"]` |
| Body | `shreddit-post [slot="text-body"] .md` (null on link/image posts) |
| Author | `[slot="authorName"] a` |
| Score | `shreddit-post faceplate-number` → prefer `number` attribute |
| Top-level comment | `shreddit-comment[depth="0"]` |
| Comment body | `shreddit-comment [slot="comment"] .md` |
| Comment meta | element attrs `author`, `score`, `created-timestamp` |

### Share links

`/s/<hash>` redirects before/as SPA mounts. Open URL → `wait_for_load` → short wait → read `location.href` for canonical path. Share links land at post top (not a deep comment).

### Comment lazy-load

Initial tree is partial. Scroll repeatedly until `shreddit-comment` count stabilizes. Expanding "more" controls is brittle; scroll is more reliable. Collapsed comments often still expose `.md` text without expand.

### Gate detection

```js
JSON.stringify({
  loginWall: !!document.querySelector('a[href*="/login"], [data-testid="login-button"]'),
  ageGate: !!document.querySelector('[data-testid="nsfw-gate"], shreddit-interstitial'),
})
```

NSFW opt-in is an account setting — do not automate preference mutation from this skill.

## Gotchas

- `faceplate-number` `innerText` is abbreviated (`1.2k`); use `getAttribute("number")`.
- `depth="0"` is a string attribute — quote in CSS selectors.
- Link/image posts: null-check text body.
- `wait_for_load` alone may see a skeleton — retry if `shreddit-post` is null.
- `old.reddit.com` is a different DOM; session cookies from new Reddit usually still apply.
- Private/quarantined/NSFW: browser session or OAuth JSON — anonymous `.json` is insufficient.
- Prefer outer `[slot="text-body"] .md` if markdown is double-wrapped.
