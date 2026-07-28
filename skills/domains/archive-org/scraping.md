---
id: archive-org-scraping
domain: archive-org
category: scraping
status: active
---

# Internet Archive / Wayback Machine — read-only extraction

`https://archive.org` / `https://web.archive.org` — public data, no auth. Prefer **official HTTP APIs** via `browser_act` `http_get`. A full browser session is not required for these workflows.

## Do this first

**Use the CDX API for anything Wayback-related.** The Wayback Availability API (`/wayback/available`) often returns empty `archived_snapshots` even for well-archived URLs — do not treat it as primary.

```text
browser_act action=http_get
  url=https://web.archive.org/cdx/search/cdx?url=iana.org&output=json&limit=5&fl=timestamp,original,statuscode,mimetype,length
  timeout=40
```

Parse the JSON body: row 0 is the header; later rows are captures. Snapshot playback URL:

```text
https://web.archive.org/web/{timestamp}/{original}
```

For item metadata (books, video, audio, software):

```text
browser_act action=http_get
  url=https://archive.org/metadata/{identifier}
  timeout=30
```

## Common workflows

### Nearest snapshot to a target date

```text
browser_act action=http_get
  url=https://web.archive.org/cdx/search/cdx?url=iana.org&output=json&limit=1&fl=timestamp,original,statuscode&closest=20230601120000&sort=closest
  timeout=60
```

Timestamp format is 14-digit `YYYYMMDDHHMMSS`. Prefixes work (`20230601`, `202306`, `2023`). CDX can be slow — use `timeout` ≥ 40s.

### Monthly snapshots (collapsed)

```text
...&collapse=timestamp:6&from=20230101&to=20240101&fl=timestamp,original
```

`collapse=timestamp:N` keeps one row per truncated timestamp prefix (`:4` year, `:6` month, `:8` day). Collapse keeps the **first** capture in the bucket.

### Domain-wide captures

```text
...&url=iana.org&matchType=domain&limit=10&collapse=timestamp:8
```

`matchType`: `exact` (default), `prefix`, `host`, `domain`.

### Paginate CDX with resumeKey

Use `showResumeKey=true` and `limit=`. Response shape with resume: data rows, then `[]`, then `[resume_key]`. Next page: `&resumeKey={key}` (do not double-encode). Yield rows excluding the header.

### Archived page body

```text
https://web.archive.org/web/{14-digit-ts}/{original-url}
```

HTML includes an injected Wayback toolbar (`BEGIN WAYBACK TOOLBAR INSERT`). Calendar UI (browser navigation only): `https://web.archive.org/web/20230101000000*/example.org`.

### Item metadata + files

Top-level keys commonly include: `metadata`, `files`, `files_count`, `server`, `dir`, `item_size`.

Useful `metadata` fields: `identifier`, `title`, `mediatype`, `creator`, `date`, `description`, `subject`, `publicdate`, `collection`.

Each file: `name`, `source` (`original`|`derivative`|`metadata`), `format`, `size` (string bytes), hashes, optional `length`/`height`/`width`.

Download URLs:

```text
https://archive.org/download/{identifier}/{filename}   # stable CDN redirect — prefer this
https://{server}{dir}/{filename}                       # direct storage node (faster, less stable)
```

### Search items

Use `advancedsearch.php` — **not** `/search` (SPA; ignores `output=json`).

```text
https://archive.org/advancedsearch.php
  ?q=artificial+intelligence+AND+mediatype:texts
  &fl[]=identifier&fl[]=title&fl[]=creator&fl[]=date&fl[]=downloads
  &rows=5&start=0&sort[]=downloads+desc&output=json
```

Pagination: `start=` offset. `rows=100` is reliable. Lucene-style `q=`. Mediatypes include: `texts`, `audio`, `movies`, `software`, `image`, `etree`, `data`, `collection`.

## API reference

| Endpoint | Returns | Auth |
|---|---|---|
| `web.archive.org/cdx/search/cdx?url=…&output=json` | Snapshot index | none |
| `archive.org/wayback/available?url=…` | Nearest snapshot (**unreliable**) | none |
| `archive.org/metadata/{identifier}` | Item metadata + files | none |
| `archive.org/advancedsearch.php?q=…&output=json` | Item search | none |
| `archive.org/download/{identifier}/{filename}` | File bytes | none |
| `web.archive.org/web/{timestamp}/{url}` | Archived HTML | none |

## CDX fields

Default `fl` order: `urlkey,timestamp,original,mimetype,statuscode,digest,length`. All values are **strings** in JSON mode. `original` may include ports (`http://example.org:80/`) — pass through when building playback URLs.

## Rate limits / posture

- CDX: often slow/timeout-prone; `timeout` 40–60s; sleep ~1s between looped CDX calls.
- Metadata / advancedsearch: generally fast.
- No API key. Stay respectful; no bulk hammering.

## Gotchas

- Always set a high timeout on CDX; retry on timeout.
- Prefer CDX `sort=closest&limit=1` over `/wayback/available`.
- With `output=json`, row 0 is the header — slice data from `rows[1:]`.
- Omit `output=json` → space-separated text, not JSON.
- Empty metadata `{}` with HTTP 200 means missing/private item.
- `subject` may be a string or a list — normalize before use.
- File `size` / media `length` are strings — cast explicitly.
- Prefer `archive.org/download/…` over raw storage hostnames.
- `/search?output=json` returns HTML; use `advancedsearch.php`.
- `from=` / `to=` accept partial timestamps; treat `to=` as exclusive end of that prefix unless you bump the bound.
