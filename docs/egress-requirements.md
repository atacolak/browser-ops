# browser egress requirements

keep proxy acquisition and credentials outside this repository. this document records only the engineering constraints a browser egress must satisfy.

## required properties

- **network class:** prefer residential or mobile asns for account-bound browser work. hosting/datacenter asns are a poor fit when the target site expects consumer traffic.
- **session stability:** use sticky egress for at least the full browser job; rotating per request breaks cookies, oauth, and risk continuity.
- **geo consistency:** keep an account near its established region. after a deliberate region change, allow a cool-off before sensitive login or oauth work.
- **protocol:** authenticated socks5 or http connect is acceptable when the browser runtime supports it.
- **secret injection:** provide the proxy url through environment or a local secret store. never commit `user:password@host:port`.
- **binding:** one browser lease records which egress it uses; do not silently change egress mid-job.

## verification

an operator-side checker may read `PROXY_URL` (or an untracked local file) and report only:

- reachability
- observed public ip
- asn / organization classification
- country / region
- latency
- sticky-session continuity

logs must redact credentials and should not retain the complete proxy url.

## excluded

this repository does not contain:

- vendor or seller contact lists
- marketplace purchase automation
- account-purchase filters
- proxy credential inventories
- checkout automation

those are acquisition concerns, not browser-control-plane capabilities.
