---
id: arxiv-scraping
domain: arxiv
category: scraping
status: active
---

# arXiv — read-only extraction

`https://arxiv.org` — open-access preprints. **Prefer HTTP APIs** (`browser_act` `http_get`) over a browser session. No API key.

Companion bulk harvest / citation enrichment: `arxiv-bulk/scraping` (OAI-PMH + Semantic Scholar).

## Do this first

**Atom API for search and metadata** — one call, XML, no auth.

```text
browser_act action=http_get
  url=http://export.arxiv.org/api/query?search_query=ti:transformer+AND+cat:cs.LG&max_results=5&sortBy=submittedDate&sortOrder=descending
```

Namespaces when parsing:

```text
atom  → http://www.w3.org/2005/Atom
arxiv → http://arxiv.org/schemas/atom
```

Known IDs: `id_list=1706.03762,1810.04805` (comma-separated batch). HTML abs pages expose `citation_*` meta tags if you need a non-XML path.

## Common workflows

### Search (Atom)

From each `atom:entry`:

| Field | Path |
|---|---|
| title | `atom:title` (strip newlines) |
| id | last path segment of `atom:id` (e.g. `1706.03762v7`) |
| published / updated | ISO date prefix |
| abstract | `atom:summary` |
| authors | `atom:author/atom:name` (`First Last`) |
| categories | `atom:category@term` |
| primary | `arxiv:primary_category@term` |
| PDF | `atom:link[@title=pdf]/href` |
| abs | `atom:link[@rel=alternate]/href` |

### Single / batch by ID

```text
.../api/query?id_list=1706.03762
.../api/query?id_list=1706.03762,1810.04805,2005.14165&max_results=3
```

Batch one call is faster than N parallel single-ID calls. Set `max_results=len(ids)`. Return order is **not** request order — index by ID. Missing IDs yield zero entries (not an HTTP error).

### HTML abs page (`citation_*` meta)

```text
browser_act action=http_get
  url=https://arxiv.org/abs/1706.03762
```

Static HTML (~tens of KB). Meta tags: `citation_title`, `citation_author` (`Last, First`, one per author), `citation_date`, `citation_online_date`, `citation_pdf_url` (versionless → latest), `citation_arxiv_id`, `citation_abstract`.

### Category search + pagination

```text
?search_query=cat:cs.AI&max_results=10&start=0&sortBy=lastUpdatedDate&sortOrder=descending
```

OpenSearch elements: `opensearch:totalResults`, `startIndex`, `itemsPerPage` (`http://a9.com/-/spec/opensearch/1.1/`).

## URL / query reference

Base: `http://export.arxiv.org/api/query` (HTTPS also works).

| Param | Notes |
|---|---|
| `search_query` | `ti:`, `au:`, `abs:`, `co:`, `jr:`, `cat:`, `all:` + `AND`/`OR`/`ANDNOT` |
| `id_list` | bare or versioned IDs, comma-separated |
| `max_results` | default 10, max 2000 |
| `start` | offset |
| `sortBy` | `relevance`, `lastUpdatedDate`, `submittedDate` |
| `sortOrder` | `ascending`, `descending` |

PDF / abs construction:

```text
bare_id = strip trailing vN from API id
https://arxiv.org/pdf/{versioned_or_bare}
https://arxiv.org/abs/{versioned_or_bare}
```

API PDF links are versioned; HTML `citation_pdf_url` is versionless (latest).

Category taxonomy: `https://arxiv.org/category_taxonomy` (e.g. `cs.LG`, `cs.CV`, `cs.AI`, `cs.CL`, `stat.ML`).

## Gotchas

- Do not open a browser for routine arXiv reads — SSR HTML + Atom cover search, authors, abstracts, PDF URLs.
- Always pass the Atom/arXiv namespace map or `findall` returns empty.
- `atom:id` is a full URL — take the last path segment.
- `arxiv:comment`, `journal_ref`, `doi` may be absent — null-check.
- Sustained crawl: ~3s between requests; bursts may get 503/429 without headers.
- Author name order differs: Atom `First Last` vs HTML meta `Last, First`.
- `sortBy=relevance` only applies with `search_query`, not `id_list`.
- Bulk date/subject harvest → use `arxiv-bulk` (OAI-PMH), not Atom pagination alone.
