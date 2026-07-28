---
id: hackernews-scraping
domain: hackernews
category: scraping
status: active
---

# Hacker News — read-only extraction

`https://news.ycombinator.com` — public aggregator. Prefer **HTTP APIs / HTML over `browser_act` `http_get`**. Do not open a browser for routine HN reads.

## Pick an access path

| Goal | Approach |
|---|---|
| Current front page (~30 stories) | `http_get` front page + regex |
| Historical / keyword search | Algolia search API |
| Nested comment tree | Algolia items API |
| Item by ID / ranked ID lists | Firebase HN API |

## Path 1: Front page HTML

```text
browser_act action=http_get url=https://news.ycombinator.com
```

```python
import re, html as htmllib

page = body  # from http_get

story_ids = re.findall(r'<tr class="athing submission" id="(\d+)">', page)
titles_urls = re.findall(
    r'class="titleline"[^>]*><a href="([^"]*)"[^>]*>(.*?)</a>', page
)
scores_by_id = {
    m.group(1): int(m.group(2))
    for m in re.finditer(
        r'<span class="score" id="score_(\d+)">(\d+) points</span>', page
    )
}
authors_by_id = {}
for m in re.finditer(
    r'<span class="score" id="score_(\d+)">\d+ points</span>'
    r'.*?class="hnuser">(.*?)</a>',
    page,
    re.DOTALL,
):
    authors_by_id[m.group(1)] = m.group(2)
comments_by_id = {
    m.group(1): int(m.group(2))
    for m in re.finditer(
        r'href="item\?id=(\d+)">(\d+)&nbsp;comments</a>', page
    )
}

stories = []
for i, sid in enumerate(story_ids):
    url, raw_title = titles_urls[i] if i < len(titles_urls) else ("", "")
    stories.append({
        "rank": i + 1,
        "id": sid,
        "title": htmllib.unescape(raw_title),
        "url": url,
        "score": scores_by_id.get(sid),  # None for job posts
        "author": authors_by_id.get(sid),
        "comments": comments_by_id.get(sid, 0),
    })
```

Notes:

- Always `html.unescape` titles.
- Story rows use class `athing submission`; comments use `athing comtr`.
- Job posts may lack score/author.
- Anchor score/author by story id — avoid brittle positional zips across `re.DOTALL`.
- `?p=2` exists; deeper pages may require a logged-in cookie (out of scope for anonymous read).

## Path 2: Algolia

```text
https://hn.algolia.com/api/v1/search?query=llm&tags=story&hitsPerPage=20
https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=20
# page=N is 0-indexed; nbPages in response
```

Story hit fields include: `objectID`, `title`, `url`, `author`, `points`, `num_comments`, `created_at`, `created_at_i`, `story_id`, `children` (flat comment ids), `_tags`.

Comment hits use **`comment_text`** (not `text`). Self-posts may use `story_text`.

Tags (AND by default; OR with parentheses): `story`, `show_hn`, `ask_hn`, `poll`, `job`, `front_page`, `author_<user>`, `story_<id>`.

Numeric filters: `created_at_i>…`, `points>100`, combinable with commas.

### Nested thread

```text
https://hn.algolia.com/api/v1/items/{id}
```

`children` is a nested tree (`author`, `text` HTML, `created_at`, `children`). Walk with a stack for totals; deleted nodes may make counts slightly under UI totals.

## Path 3: Firebase API

```text
https://hacker-news.firebaseio.com/v0/topstories.json   # ~500 ids
https://hacker-news.firebaseio.com/v0/newstories.json
https://hacker-news.firebaseio.com/v0/beststories.json
https://hacker-news.firebaseio.com/v0/askstories.json
https://hacker-news.firebaseio.com/v0/showstories.json
https://hacker-news.firebaseio.com/v0/jobstories.json
https://hacker-news.firebaseio.com/v0/item/{id}.json
https://hacker-news.firebaseio.com/v0/user/{id}.json
https://hacker-news.firebaseio.com/v0/maxitem.json
```

Item fields: `id`, `type`, `by`, `title`, `url`, `score`, `descendants`, `time`, `kids`, `text`. User: `id`, `karma`, `created`, `about`, `submitted`.

Tradeoff: ID lists are cheap; hydrating hundreds of items needs one request each. For top-30 full rows, front-page HTML wins; for rich search, Algolia wins.

## Item page HTML

```text
https://news.ycombinator.com/item?id={id}
```

All comments can appear in one large HTML response (`athing comtr`). Prefer Algolia items for structured trees; HTML is mainly for crude counts.

## Gotchas

- Prefer APIs/`http_get` over `navigate` + `evaluate` — orders of magnitude faster, no benefit to DOM automation for public HN.
- Do not use legacy `goto_url` / bare `js()` names in procedures — map to `browser_act` actions if a browser is unavoidable.
- No auth for the paths above; stay read-only (no voting/commenting automation in this skill).
