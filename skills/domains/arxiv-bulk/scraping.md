---
id: arxiv-bulk-scraping
domain: arxiv-bulk
category: scraping
status: active
---

# arXiv bulk harvest + Semantic Scholar — read-only

Companion to `arxiv/scraping`. Use **arxiv** for search/fetch by keyword or ID. Use **this skill** for:

- Bulk harvest by subject/date (OAI-PMH)
- Citation counts and cross-database IDs (Semantic Scholar)
- Per-paper version history / submitter (`arXivRaw`)

No API key required for the unauthenticated paths below. Prefer `browser_act` `http_get` (and plain HTTP POST only where noted). Not a browser-DOM workflow.

## OAI-PMH bulk harvest

Canonical endpoint:

```text
https://oaipmh.arxiv.org/oai
```

Older `https://export.arxiv.org/oai2` 301-redirects here. Prefer the canonical URL so clients that do not follow redirects still work.

### Harvest loop (ListRecords)

```text
GET https://oaipmh.arxiv.org/oai
  ?verb=ListRecords
  &metadataPrefix=arXiv
  &set=cs
  &from=YYYY-MM-DD
  &until=YYYY-MM-DD
```

Parse XML with namespaces:

```text
oai  → http://www.openarchives.org/OAI/2.0/
arXiv → http://arxiv.org/OAI/arXiv/
```

For each `oai:record`:

- Skip deleted rows (`header@status=deleted` or missing `arXiv:arXiv` metadata).
- Fields: `id`, header `datestamp`, `created`, `updated`, `title`, authors (`forenames` + `keyname`), `categories` (space-split), `abstract`, `doi`, `journal-ref`, `license`.

Pagination: read `oai:resumptionToken` text; next request is **only**

```text
?verb=ListRecords&resumptionToken={token}
```

Pass the token **verbatim** (already encoded). Sleep **≥5s** between pages (policy / token validity).

### Verbs

| Verb | Purpose |
|---|---|
| `Identify` | Repo info, earliest datestamp |
| `ListSets` | Harvestable sets |
| `ListMetadataFormats` | `oai_dc`, `arXiv`, `arXivOld`, `arXivRaw` |
| `ListRecords` | Bulk with `metadataPrefix`, `set`, `from`, `until` |
| `GetRecord` | Single `identifier` + `metadataPrefix` |

### Sets

Top-level: `cs`, `math`, `physics`, `stat`, `eess`, `econ`, `q-bio`, `q-fin`. Subsets use colon hierarchy, e.g. `cs:cs:LG` (not Atom's `cs.LG`). `set=cs.LG` returns nothing.

### Metadata formats

- **`arXiv`** — preferred rich record (structured authors, abstract, doi, …).
- **`arXivRaw`** — submitter + per-version history; authors often a flat string.
- `oai_dc` / `arXivOld` — usually skip.

### GetRecord + arXivRaw

```text
?verb=GetRecord&metadataPrefix=arXivRaw&identifier=oai:arXiv.org:1706.03762
```

Namespace: `http://arxiv.org/OAI/arXivRaw/`. Read `title`, `submitter`, each `version@version` + `date`.

## Semantic Scholar enrichment

Base: `https://api.semanticscholar.org/graph/v1/`

Unauthenticated: about 1 req/s and a daily cap; free API key raises limits. Prefer batch over N singles.

### Single paper by arXiv ID

```text
browser_act action=http_get
  url=https://api.semanticscholar.org/graph/v1/paper/arXiv:1706.03762?fields=title,year,venue,publicationDate,citationCount,influentialCitationCount,authors,abstract,externalIds
```

ID form `arXiv:NNNN.NNNNN` is accepted directly.

### Batch (POST, up to 500 IDs)

`browser_act` `http_get` is GET-only. For batch, use a one-shot HTTP POST from the agent environment:

```text
POST https://api.semanticscholar.org/graph/v1/paper/batch?fields=paperId,externalIds,title,year,citationCount,influentialCitationCount
Content-Type: application/json
{"ids":["arXiv:1706.03762","arXiv:1810.04805"]}
```

### Search

```text
.../paper/search?query=large+language+model&fields=paperId,externalIds,title,year,citationCount&limit=5
```

Paginate with `offset=`. Useful fields: `paperId`, `externalIds` (`ArXiv`, `DOI`, …), `title`, `abstract`, `year`, `publicationDate`, `venue`, `citationCount`, `influentialCitationCount`, `authors`, `references`, `citations`, `openAccessPdf`.

## PDF download

```text
https://arxiv.org/pdf/{bare_or_versioned_id}
```

Versionless resolves to latest. Fetch with `browser_act` `http_get` or a binary-capable HTTP client; save bytes to a path you control (no host-specific hardcoding required).

## Gotchas

- Always use `https://oaipmh.arxiv.org/oai` — avoid depending on redirect-following.
- OAI: ≥5s between resumption pages or tokens invalidate.
- OAI `datestamp` is last-modified, not first submission — use inner `created`/`updated`.
- Deleted records lack metadata — null-check.
- Author shape differs: OAI structured keyname/forenames vs Atom `First Last` vs Raw flat string.
- S2: sleep ~1s unauthenticated; 429 on bursts; batch POST counts as one request.
- `externalIds` may omit `ArXiv` — use `.get`.
- Atom sustained crawl ~1 req / 3s; OAI is the bulk path.
- OAI `set` uses colons (`cs:cs:LG`), not dots.

## When to use which skill

| Task | Skill |
|---|---|
| Keyword / author / category search | `arxiv` (Atom) |
| 1–2000 IDs | `arxiv` (`id_list`) |
| All papers in set + date window | **this** (OAI-PMH) |
| Citation / influential counts | **this** (S2) |
| Version history / submitter | **this** (`arXivRaw`) |
| PDF bytes | either (same URL) |
