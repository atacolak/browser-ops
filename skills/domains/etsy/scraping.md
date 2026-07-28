---
id: etsy-scraping
domain: etsy
category: scraping
status: active
---

# Etsy — read-only listing & shop extraction

Read-only. No purchase, cart, messaging, or seller mutation.

## Critical: DataDome on HTML

Plain `http_get` against user-facing HTML (`/search`, `/listing/`, `/shop/`, `/c/`, `/market/`) returns **HTTP 403** with **DataDome** JS challenge headers/body. UA spoofing and cookie replay do not clear it — TLS fingerprint + JS execution are required.

- Challenge body is small HTML pointing at `geo.captcha-delivery.com` (`rt: c` = challenge).
- `robots.txt` is reachable without DataDome and documents URL shapes / disallowed params.
- **Official API** `openapi.etsy.com/v3/` is not DataDome-gated but needs a developer API key from [developer.etsy.com](https://developer.etsy.com/).

**Prefer in order:**

1. Etsy API v3 when a key is available (structured JSON, no browser).
2. Browser harness for HTML/JSON-LD when you must scrape the site UI.

Do not claim bare `http_get` works on Etsy HTML.

## Official API v3 (preferred when keyed)

```text
GET https://openapi.etsy.com/v3/application/{path}?…
Header: x-api-key: <key from env / operator secret store — never commit>
```

Examples:

```text
listings/active?keywords=handmade+candle&limit=25&sort_on=created&sort_order=desc
listings/{listing_id}
shops/{shop_id_or_name}/listings/active?limit=100
shops/{shop_id_or_name}
listings/{listing_id}/images
listings/{listing_id}/reviews
seller-taxonomy/nodes
```

Useful listing fields: `listing_id`, `title`, `description`, `price.amount` + `price.divisor` + `price.currency_code` (price = amount/divisor), `quantity`, `tags`, `materials`, `shop_id`, `url`, `views`, `num_favorers`, `is_digital`, `has_variations`, `taxonomy_id`, `state`, timestamps.

Pagination: `limit` (max 100) + `offset`. Free tier is rate-limited by day — small sleeps between calls are polite.

Errors without/invalid key return HTTP 403 JSON about API key format/status.

## Browser path (HTML / JSON-LD)

Use navigator tools only:

```text
browser_act action=new_tab url=https://www.etsy.com/search?q=handmade+candle&explicit=1
browser_act action=wait_for_load
# SPA: brief extra wait after readyState if cards still empty
browser_act action=evaluate expression=<JS below>
```

Or `navigate` on an existing tab. Prefer `evaluate` / `extract` over nonexistent legacy JS/goto helper wrappers.

### Search URL params

```text
https://www.etsy.com/search?q={query}&explicit=1
```

- `explicit=1` — include results the NSFW filter would drop; safe default for general queries.
- `page=N` — ~48 results/page.
- `min_price` / `max_price`, `order=price_asc|price_desc|most_relevant|newest`, `listing_type=handmade|vintage|supplies`.

Robots disallows several facet params (`attr_*`, `price_bucket`, `ship_to`, `search_type`) — avoid those in automated crawls even in-browser when possible.

### Search extraction

DOM cards:

```js
JSON.stringify(
  Array.from(document.querySelectorAll("[data-listing-id]")).map((el) => ({
    listing_id: el.getAttribute("data-listing-id"),
    title:
      el.querySelector('h3, [class*="listing-link"]')?.innerText?.trim() ||
      el.querySelector("h2")?.innerText?.trim(),
    price:
      el.querySelector('[class*="currency-value"]')?.innerText?.trim() ||
      el.querySelector(".currency-value")?.innerText?.trim(),
    shop: el.querySelector('[class*="shop-name"], [data-shop-name]')?.innerText?.trim(),
    url: el.querySelector('a[href*="/listing/"]')?.href,
    thumbnail: el.querySelector('img[src*="etsystatic"]')?.src,
    is_ad: !!(el.querySelector('[class*="ad-label"], [class*="sponsored"]')),
  })).filter((r) => r.listing_id)
)
```

Prefer JSON-LD `ItemList` when present (URL/name/position; often ~48 items — no price):

```js
JSON.stringify(
  Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .map((s) => { try { return JSON.parse(s.textContent); } catch (e) { return null; } })
    .filter((d) => d && d["@type"] === "ItemList")[0] || null
)
```

### Listing detail

```text
browser_act action=navigate url=https://www.etsy.com/listing/{id}
browser_act action=wait_for_load
```

JSON-LD `Product`: `name`, `description`, `offers.price` (string), `offers.priceCurrency`, `offers.availability`, `brand.name` or `seller.name`, `aggregateRating`, `image[]`.

DOM fallbacks (fragile class names): `h1[data-buy-box-listing-title]`, `[data-selector="price-only"]`, shop link `a[href*="/shop/"]`.

### Shop page

```text
https://www.etsy.com/shop/{ShopName}
```

JSON-LD may be `LocalBusiness` / `Store` / `Organization`. DOM: shop name, sales count, admirers, location, `[data-listing-id]` count. More listings: scroll (`browser_act action=scroll`) or follow a load-more control if present — read-only.

### Pagination

```text
?page=N on search, or rel=next / data-wt-search-page-next href
```

## URL patterns

| URL | Purpose |
|---|---|
| `/search?q=…&explicit=1` | keyword search |
| `/listing/{id}/{slug?}` | listing detail (slug optional) |
| `/shop/{ShopName}` | shop home |
| `/c/…` | category browse |
| `/market/{keyword}` | tag/market page |
| `openapi.etsy.com/v3/application/…` | official API |

## Gotchas

- HTML without browser ≈ always DataDome 403.
- Shop path casing can matter — use exact shop slug from listing links.
- JSON-LD price is a string; API price is subunit integers.
- ItemList alone is not enough for price/rating — open listing pages or use API.
- `/market/` layout differs from `/search` but same bot gate.
- Unescape HTML entities in API descriptions when displaying.
- Never embed API keys in skills or commits — load from environment/operator store.
