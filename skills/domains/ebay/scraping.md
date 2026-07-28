---
id: ebay-scraping
domain: ebay
category: scraping
status: active
---

# eBay — read-only search & listing extraction

Public listing pages. No purchase, bid, message, or account-mutation flows.

Prefer **`browser_act` `http_get`** for HTML when unblocked. If bot detection trips, back off or use the browser harness (`navigate` / `evaluate` / `extract`) with the same parsers — do not invent non-harness wrappers.

## Bot detection ("Pardon Our Interruption")

After roughly several rapid requests per IP, eBay may return a small interstitial (~tens of KB) titled **"Pardon Our Interruption…"** with no listing data. This is **not** a CAPTCHA solve flow — back off 60–120s and retry.

```python
def is_blocked(html: str) -> bool:
    return "Pardon Our Interruption" in html or len(html) < 20_000
```

Suggested headers for `http_get`:

```text
User-Agent: full desktop Chrome UA string
Accept-Language: en-US,en;q=0.9
Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
```

Minimal UA strings trip blocks sooner. Between paginated requests, sleep ~3–5s.

## Search URL structure

```text
https://www.ebay.com/sch/i.html?_nkw={query}&{filters}
```

| Parameter | Effect |
|---|---|
| `LH_BIN=1` | Buy It Now only |
| `LH_Auction=1` | Auctions only |
| `LH_ItemCondition=` | condition code (see below) |
| `_sop=` | sort code |
| `_pgn=` | page number (~65–88 items/page observed) |
| `_ipg=` | items per page (25/50/100/200 — standard param) |

Condition codes: `1000` New, `1500` New other, `2000` Mfr refurbished, `2500` Seller refurbished, `2750` Like new, `3000` Used, `4000` Very good, `5000` Good, `6000` Acceptable, `7000` For parts.

Sort `_sop`: `1` Best match, `10` Ending soonest, `12` Newly listed, `15` Lowest price+shipping, `16` Highest price.

Item detail: `https://www.ebay.com/itm/{listing_id}` — strip query/tracking params from extracted links.

## Search results HTML (no JSON-LD)

Search pages embed cards in `<li data-listingid=…>` (~1.5–1.8 MB uncompressed). JSON-LD is **absent** on search; present on item pages.

### Extractor pattern

```python
import re

def extract_search_results(html):
    if "Pardon Our Interruption" in html or len(html) < 20_000:
        return []
    cards = re.split(r"(?=<li[^>]+data-listingid=)", html)
    results, seen = [], set()
    for card in cards[1:]:
        lid_m = re.search(r"data-listingid=(\d+)", card)
        if not lid_m:
            continue
        listing_id = lid_m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        url_m = re.search(r"href=(https://(?:www\.)?ebay\.com/itm/(\d+))", card)
        item_url = url_m.group(1).split("?")[0] if url_m else None
        title_m = re.search(r"s-card__title[^>]*>.*?primary[^>]*>([^<]+)", card, re.DOTALL)
        title = title_m.group(1).strip() if title_m else None
        if not title or title == "Shop on eBay":
            continue
        price_m = re.search(r'class=(?:["\'])?[a-z- ]*price["\']?>\$([0-9,\.]+)<', card)
        if not price_m:
            price_m = re.search(r'price">\$([0-9,\.]+)<', card)
        price = "$" + price_m.group(1) if price_m else None
        orig_m = re.search(r"strikethrough[^>]*>\$([0-9,\.]+)", card)
        original_price = "$" + orig_m.group(1) if orig_m else None
        img_m = re.search(r"class=s-card__image[^>]*src=([^\s>]+)", card)
        image = img_m.group(1) if img_m else None
        results.append({
            "listing_id": listing_id,
            "url": item_url,
            "title": title,
            "price": price,
            "original_price": original_price,
            "image": image,
        })
    return results
```

Usage sketch:

```text
browser_act action=http_get
  url=https://www.ebay.com/sch/i.html?_nkw=mechanical+keyboard&LH_BIN=1&_sop=15
# then extract_search_results(body)
```

## Item detail: JSON-LD Product

Two JSON-LD blocks common: `BreadcrumbList` and `Product`. Prefer Product for price/condition/availability/brand/images/returns.

```python
import re, json

def extract_item_detail(html):
    if "Pardon Our Interruption" in html:
        return None
    ld_blocks = re.findall(r"application/ld\+json[^>]*>(.*?)</script>", html, re.DOTALL)
    product, breadcrumbs = None, []
    for ld_str in ld_blocks:
        try:
            d = json.loads(ld_str.strip())
        except Exception:
            continue
        if d.get("@type") == "Product":
            product = d
        elif d.get("@type") == "BreadcrumbList":
            breadcrumbs = [i.get("name") for i in d.get("itemListElement", [])]
    if not product:
        return None
    offers = product.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    CONDITION_MAP = {
        "NewCondition": "New",
        "UsedCondition": "Used",
        "RefurbishedCondition": "Refurbished",
        "DamagedCondition": "For Parts / Not Working",
        "LikeNewCondition": "Like New",
        "VeryGoodCondition": "Very Good",
        "GoodCondition": "Good",
        "AcceptableCondition": "Acceptable",
    }
    cond_url = offers.get("itemCondition", "") or ""
    cond_key = cond_url.rstrip("/").split("/")[-1]
    condition = CONDITION_MAP.get(cond_key, cond_key)
    price_spec = offers.get("priceSpecification", {}) or {}
    list_price = price_spec.get("price") if price_spec.get("name") == "List Price" else None
    shipping_details = offers.get("shippingDetails", []) or []
    if shipping_details:
        shipping_val = shipping_details[0].get("shippingRate", {}).get("value", "")
        shipping = "Free" if str(shipping_val) in ("0", "0.0") else f"${shipping_val}"
    else:
        shipping = None
    return_policies = offers.get("hasMerchantReturnPolicy", []) or []
    return_days = return_policies[0].get("merchantReturnDays") if return_policies else None
    brand = product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    return {
        "listing_id": (offers.get("url") or "").split("/itm/")[-1].split("?")[0],
        "name": product.get("name"),
        "brand": brand,
        "price": offers.get("price"),
        "list_price": list_price,
        "currency": offers.get("priceCurrency"),
        "availability": (offers.get("availability") or "").split("/")[-1],
        "condition": condition,
        "shipping": shipping,
        "return_days": return_days,
        "images": product.get("image", []),
        "gtin13": product.get("gtin13"),
        "mpn": product.get("mpn"),
        "color": product.get("color"),
        "breadcrumbs": breadcrumbs,
    }
```

Seller name / feedback / sold counts are **not** in JSON-LD — scrape complementary `ux-textspans` text if needed (indices vary; treat as unordered labels, not fixed offsets).

## Pagination workflow

1. `http_get` search URL; abort if blocked.
2. Parse cards; optionally `http_get` top item URLs with 3s+ delays.
3. Stop the loop on first block page.

## Official APIs

| API | Notes |
|---|---|
| Finding (`svcs.ebay.com`) | historically broken / non-viable unauthenticated |
| Browse (`api.ebay.com`) | OAuth developer token |
| Shopping (`open.api.ebay.com`) | token required |
| RSS (`_rss=1`) | same bot gate as HTML |

There is no reliable public unauthenticated product API — HTML (or authenticated Browse) is the path.

## Gotchas

- Deduplicate listing IDs — each card repeats IDs multiple times.
- Filter placeholder "Shop on eBay" / stub listing ids.
- Normalize `//ebay.com/` → `//www.ebay.com/`; strip `?` tracking.
- Prefer `re.split` on card boundaries over heavy HTML parsers for large search HTML.
- `itemCondition` is a schema.org URL — map the last path segment.
- `list_price` only when a "List Price" comparison is shown.
- Do not treat interstitial HTML as empty inventory.
