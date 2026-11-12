# 公共研究通告归档管线 · 端到端演示报告

生成时间：2026-11-12T12:52:18

所有请求仅发往本地模拟站点 `127.0.0.1:8811`(sim_a) 与 `127.0.0.1:8812`(sim_b)。

## 步骤 1 · 基线扫描：建立首个版本与请求清单

两站各自完整扫描。A 站目录链接带业务参数 `sort=asc&view=full` 与追踪参数；B 站目录使用相对短链 `n/<slug>?b=2&a=1&utm_*`，服务器以 302 跳到长地址。所有详情均解析为统一字段 `source/document_key/title/issued_on/body/canonical_url`。

**扫描清单**

### scan 1 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | ok | 200 | A-1001 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | ok | 200 | A-1002 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | ok | 200 | A-1002P | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | ok | 200 | A-1003 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | ok | 200 | A-1044 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | ok | 200 | A-1055 | v1（新） | first_observation |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full: canonical https://mirror.example.net/notices/crossed-canonical/A-1003 is off-origin; recorded but never requested

请求清单文件：`reports/requests/scan-1.json`

### scan 2 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 4，详情请求: 5，实际 HTTP 请求: 15，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` |
| 3 | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` |
| 4 | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | ok | 200 | B-2001 | v1（新） | first_observation |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | ok | 200 | B-2002 | v1（新） | first_observation |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | ok | 200 | B-2003 | v1（新） | first_observation |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |
| `http://127.0.0.1:8812/n/flaky?a=1&b=2` | ok | 200 | B-2005 | v1（新） | first_observation |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/flaky?a=1&b=2 -> http://127.0.0.1:8812/archive/flaky/B-2005?a=1&b=2 (in-scope target, followed)

请求清单文件：`reports/requests/scan-2.json`

## 步骤 2 · 等价链接归一、保留业务参数、站外与 robots 拦截

归一化只删除配置内的追踪键、排序剩余参数并去掉 fragment；不同业务参数（`edition=public`）不合并。站外链接与 robots 禁止路径均未产生 HTTP 请求（清单中 `requested=false`）。`rel=canonical` 指向站外主机时只记录、不跟随请求；服务器日志交叉核验证明了实际到达的路径集合。

**扫描清单**

### scan 1 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | ok | 200 | A-1001 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | ok | 200 | A-1002 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | ok | 200 | A-1002P | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | ok | 200 | A-1003 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | ok | 200 | A-1044 | v1（新） | first_observation |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | ok | 200 | A-1055 | v1（新） | first_observation |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full: canonical https://mirror.example.net/notices/crossed-canonical/A-1003 is off-origin; recorded but never requested

请求清单文件：`reports/requests/scan-1.json`

### scan 2 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 4，详情请求: 5，实际 HTTP 请求: 15，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` |
| 3 | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` |
| 4 | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | ok | 200 | B-2001 | v1（新） | first_observation |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | ok | 200 | B-2002 | v1（新） | first_observation |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | ok | 200 | B-2003 | v1（新） | first_observation |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |
| `http://127.0.0.1:8812/n/flaky?a=1&b=2` | ok | 200 | B-2005 | v1（新） | first_observation |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/flaky?a=1&b=2 -> http://127.0.0.1:8812/archive/flaky/B-2005?a=1&b=2 (in-scope target, followed)

请求清单文件：`reports/requests/scan-2.json`

**证据**

```json
{
  "equivalent": [
    {
      "equivalent_count": 1,
      "detail_urls": [
        "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full"
      ],
      "note": "目录同时出现 A-1001 的两种等价写法（排序不同+追踪参数+fragment）；frontier 只保留一条并只请求一次。"
    }
  ],
  "business_parameter": [
    {
      "detail_urls": [
        "http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public"
      ],
      "note": "`edition=public` 不在 tracking_params 中，因此保留为不同frontier 键并分别生成文档 A-1002 与 A-1002P。",
      "document_keys": [
        "A-1002P"
      ]
    }
  ],
  "offsite_refused": [
    {
      "url": "https://example.invalid/offsite",
      "where": "detail link on http://127.0.0.1:8811/notices",
      "reason": "origin_not_allowed"
    }
  ],
  "robots_refused": [
    {
      "url": "http://127.0.0.1:8811/notices/draft-notes/D-77",
      "where": "detail link on http://127.0.0.1:8811/notices",
      "reason": "robots_disallowed"
    }
  ],
  "offsite_canonical": [
    {
      "url": "http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full",
      "canonical_url": "https://mirror.example.net/notices/crossed-canonical/A-1003",
      "canonical_blocked": "https://mirror.example.net/notices/crossed-canonical/A-1003",
      "requested": false
    }
  ],
  "redirects": [],
  "server_side_audit": {
    "layout_a": {
      "requests_served": 10,
      "unique_paths": [
        "/notices",
        "/notices/crossed-canonical/A-1003?sort=asc&view=full",
        "/notices/page/2?sort=asc&view=list",
        "/notices/page/3?sort=asc&view=list",
        "/notices/public-edition/A-1002?edition=public",
        "/notices/public-edition/A-1002?sort=asc&view=full",
        "/notices/retractable/A-1044?sort=asc&view=full",
        "/notices/same-url/A-1001?sort=asc&view=full",
        "/notices/temporary/A-1055?sort=asc&view=full",
        "/robots.txt"
      ],
      "non_local_paths": []
    },
    "layout_b": {
      "requests_served": 15,
      "unique_paths": [
        "/archive/ethics-guidance/B-2003?a=1&b=2",
        "/archive/flaky/B-2005?a=1&b=2",
        "/archive/grant-round/B-2002?a=1&b=2",
        "/archive/offsite-hop/B-2004?a=1&b=2",
        "/archive/water-quality/B-2001?a=1&b=2",
        "/feed",
        "/feed?page=2&sort=asc&view=list",
        "/feed?page=3&sort=asc&view=list",
        "/feed?page=4&sort=asc&view=list",
        "/n/ethics-guidance?a=1&b=2",
        "/n/flaky?a=1&b=2",
        "/n/grant-round?a=1&b=2",
        "/n/offsite-hop?a=1&b=2",
        "/n/water-quality?a=1&b=2",
        "/robots.txt"
      ],
      "non_local_paths": []
    }
  }
}
```

## 步骤 3 · 第二轮扫描：条件抓取与 304

详情请求携带上一轮的 `If-None-Match` / `If-Modified-Since`，服务器返回 304。304 不生成版本、不把正文当作空、复用既有版本；304 的空响应体仍按摘要归档（sha256 e3b0c442…）。

**扫描清单**

### scan 3 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | not_modified | 304 | A-1055 | 沿用 v1 | http_304_reused_version |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full

请求清单文件：`reports/requests/scan-3.json`

### scan 4 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 4，详情请求: 5，实际 HTTP 请求: 15，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` |
| 3 | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` |
| 4 | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | not_modified | 304 | B-2001 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | not_modified | 304 | B-2002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | not_modified | 304 | B-2003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |
| `http://127.0.0.1:8812/n/flaky?a=1&b=2` | not_modified | 304 | B-2005 | 沿用 v1 | http_304_reused_version |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/flaky?a=1&b=2
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/flaky?a=1&b=2 -> http://127.0.0.1:8812/archive/flaky/B-2005?a=1&b=2 (in-scope target, followed)

请求清单文件：`reports/requests/scan-4.json`

**证据**

```json
{
  "not_modified": [
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/grant-round?a=1&b=2",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/flaky?a=1&b=2",
      "status": 304,
      "version": 1,
      "raw_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "note": "HTTP 304: reused archived version 1; no version written; 304 body (0 bytes, sha256 e3b0c44298fc…) archived"
    }
  ]
}
```

## 步骤 4 · 第三轮扫描：仅页脚变化 → 不生成版本

两站只改页脚（A 增加帮助/关于链接，B 增加 status 链接），正文、标题、日期全部不变。下面的证据表明：原始字节摘要变了（raw_bytes_changed=true），业务摘要不变，因此 `version_created=false`，版本号沿用。

**扫描清单**

### scan 5 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | ok | 200 | A-1001 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | ok | 200 | A-1002 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | ok | 200 | A-1002P | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | ok | 200 | A-1003 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | ok | 200 | A-1044 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | ok | 200 | A-1055 | 沿用 v1 | business_fields_unchanged |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full: canonical https://mirror.example.net/notices/crossed-canonical/A-1003 is off-origin; recorded but never requested

请求清单文件：`reports/requests/scan-5.json`

### scan 6 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 4，详情请求: 5，实际 HTTP 请求: 15，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` |
| 3 | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` |
| 4 | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | ok | 200 | B-2001 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | ok | 200 | B-2002 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | ok | 200 | B-2003 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |
| `http://127.0.0.1:8812/n/flaky?a=1&b=2` | ok | 200 | B-2005 | 沿用 v1 | business_fields_unchanged |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/flaky?a=1&b=2
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/flaky?a=1&b=2 -> http://127.0.0.1:8812/archive/flaky/B-2005?a=1&b=2 (in-scope target, followed)

请求清单文件：`reports/requests/scan-6.json`

**证据**

```json
{
  "footer_only": [
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
      "raw_digest": "e33ad6f872f3",
      "previous_raw_digest": "c790ae5b23aa",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full",
      "raw_digest": "a9073509dfd6",
      "previous_raw_digest": "a8a9bde5f829",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public",
      "raw_digest": "8cfbde96e72b",
      "previous_raw_digest": "56da89badfc9",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full",
      "raw_digest": "26c6c2a1f4f7",
      "previous_raw_digest": "11876cdecc46",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full",
      "raw_digest": "c9db92614755",
      "previous_raw_digest": "865db7feaabb",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full",
      "raw_digest": "9e7246d50804",
      "previous_raw_digest": "bda2bbbffe84",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
      "raw_digest": "21f7594a767a",
      "previous_raw_digest": "5a51be24b44d",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/grant-round?a=1&b=2",
      "raw_digest": "fb93da8b665a",
      "previous_raw_digest": "f48ee071152a",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2",
      "raw_digest": "c337696ad839",
      "previous_raw_digest": "30647a947bd0",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/flaky?a=1&b=2",
      "raw_digest": "30d56a00eed6",
      "previous_raw_digest": "a08972ffc6ce",
      "raw_bytes_changed": true,
      "version_created": false,
      "reason": "business_fields_unchanged"
    }
  ]
}
```

## 步骤 5 · 第四轮扫描：正文与标题业务变更 → 生成新版本

A-1001 正文修改并新增段落；B-2001 标题修改。两者均产生新版本，段落级差异写入 diffs/。注意：A 站页脚仍为第二版（与上一轮相同），因此这些版本由业务字段而非页脚触发。

**扫描清单**

### scan 7 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | ok | 200 | A-1001 | v2（新） | business_fields_changed:body |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | not_modified | 304 | A-1055 | 沿用 v1 | http_304_reused_version |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full

请求清单文件：`reports/requests/scan-7.json`

### scan 8 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 4，详情请求: 5，实际 HTTP 请求: 15，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` |
| 3 | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` |
| 4 | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | ok | 200 | B-2001 | v2（新） | business_fields_changed:title |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | not_modified | 304 | B-2002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | not_modified | 304 | B-2003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |
| `http://127.0.0.1:8812/n/flaky?a=1&b=2` | not_modified | 304 | B-2005 | 沿用 v1 | http_304_reused_version |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/flaky?a=1&b=2
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/flaky?a=1&b=2 -> http://127.0.0.1:8812/archive/flaky/B-2005?a=1&b=2 (in-scope target, followed)

请求清单文件：`reports/requests/scan-8.json`

**证据**

```json
{
  "new_versions": [
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
      "document": "A-1001",
      "version": 2,
      "created": true,
      "reason": "business_fields_changed:body",
      "diff": {
        "paragraphs_added": [
          "经研究决定，自本通告发布之日起启动公共数据共享试点，试点期延长为两年。",
          "新增：试点评估报告于每季度末发布。"
        ],
        "paragraphs_removed": [
          "经研究决定，自本通告发布之日起启动公共数据共享试点，试点期为一年。"
        ]
      }
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
      "document": "B-2001",
      "version": 2,
      "created": true,
      "reason": "business_fields_changed:title",
      "diff": {
        "paragraphs_added": [],
        "paragraphs_removed": []
      }
    }
  ]
}
```

## 步骤 6 · 第五轮扫描：仅导航变化 → 不生成版本

两站导航条新增链接，页脚保持上一轮版本。原始摘要再次变化，业务摘要不变，版本数保持不变。

**扫描清单**

### scan 9 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | ok | 200 | A-1001 | 沿用 v2 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | ok | 200 | A-1002 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | ok | 200 | A-1002P | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | ok | 200 | A-1003 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | ok | 200 | A-1044 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | ok | 200 | A-1055 | 沿用 v1 | business_fields_unchanged |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full: canonical https://mirror.example.net/notices/crossed-canonical/A-1003 is off-origin; recorded but never requested

请求清单文件：`reports/requests/scan-9.json`

### scan 10 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 4，详情请求: 5，实际 HTTP 请求: 15，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` |
| 3 | `http://127.0.0.1:8812/feed?page=3&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` |
| 4 | `http://127.0.0.1:8812/feed?page=4&sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | ok | 200 | B-2001 | 沿用 v2 | business_fields_unchanged |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | ok | 200 | B-2002 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | ok | 200 | B-2003 | 沿用 v1 | business_fields_unchanged |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |
| `http://127.0.0.1:8812/n/flaky?a=1&b=2` | ok | 200 | B-2005 | 沿用 v1 | business_fields_unchanged |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/flaky?a=1&b=2
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/flaky?a=1&b=2 -> http://127.0.0.1:8812/archive/flaky/B-2005?a=1&b=2 (in-scope target, followed)

请求清单文件：`reports/requests/scan-10.json`

**证据**

```json
{
  "navigation_only": [
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_a",
      "url": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/grant-round?a=1&b=2",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    },
    {
      "source": "sim_b",
      "url": "http://127.0.0.1:8812/n/flaky?a=1&b=2",
      "raw_bytes_changed": true,
      "created": false,
      "reason": "business_fields_unchanged"
    }
  ]
}
```

## 步骤 7 · 第六轮扫描：robots 禁抓目录页 → 不请求并声明不完整

A 站 robots.txt 将 `/notices/page/3?` 列为 Disallow（精确路径，`?` 不匹配额外查询参数）。第 2 页的 rel=next 指向第 3 页，但该 URL 在发出请求前即被拒绝（清单中 `requested=false`）。遍历无法到达目录末尾，扫描标记 `complete=false` / `paging_blocked`，而不是把缺失页当作不存在。

**扫描清单**

### scan 11 — sim_a

- complete: `False` / paging_loop: `False` / stop_reason: `paging_blocked: next directory page http://127.0.0.1:8811/notices/page/3?sort=asc&view=list was refused (out of scope or robots-disallowed)`
- 页面数: 2，详情请求: 5，实际 HTTP 请求: 8，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` — robots_disallowed (next link on http://127.0.0.1:8811/notices/page/2?sort=asc&view=list)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- next link on http://127.0.0.1:8811/notices/page/2?sort=asc&view=list: http://127.0.0.1:8811/notices/page/3?sort=asc&view=list disallowed by robots.txt; no request made
- paging_blocked: next directory page http://127.0.0.1:8811/notices/page/3?sort=asc&view=list was refused (out of scope or robots-disallowed)
- unvisited directory successors remain; the listing of this scan is incomplete
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- scan incomplete: no 404 in this scan can count toward withdrawal; documents remain temporarily_unreachable at most

请求清单文件：`reports/requests/scan-11.json`

**证据**

```json
{
  "stop_reason": "paging_blocked: next directory page http://127.0.0.1:8811/notices/page/3?sort=asc&view=list was refused (out of scope or robots-disallowed)",
  "complete": false,
  "pages": [
    {
      "page_no": 1,
      "url": "http://127.0.0.1:8811/notices",
      "outcome": "ok",
      "next": [
        "http://127.0.0.1:8811/notices/page/2?sort=asc&view=list"
      ],
      "details": [
        "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
        "http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full",
        "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
        "http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public"
      ]
    },
    {
      "page_no": 2,
      "url": "http://127.0.0.1:8811/notices/page/2?sort=asc&view=list",
      "outcome": "ok",
      "next": [
        "http://127.0.0.1:8811/notices/page/3?sort=asc&view=list"
      ],
      "details": [
        "http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full",
        "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full"
      ]
    }
  ],
  "refused": [
    {
      "url": "http://127.0.0.1:8811/notices/page/3?sort=asc&view=list",
      "where": "next link on http://127.0.0.1:8811/notices/page/2?sort=asc&view=list",
      "reason": "robots_disallowed"
    }
  ],
  "robots_rules": [
    {
      "agents": [
        "*"
      ],
      "rules": [
        {
          "allow": false,
          "pattern": "/admin/"
        },
        {
          "allow": false,
          "pattern": "/private/"
        },
        {
          "allow": false,
          "pattern": "/notices/page/3?"
        },
        {
          "allow": false,
          "pattern": "/notices/draft-notes"
        },
        {
          "allow": true,
          "pattern": "/notices/"
        },
        {
          "allow": true,
          "pattern": "/notices/page/"
        }
      ]
    }
  ]
}
```

## 步骤 8 · 第七轮扫描：目录分页环 → 终止并报告不完整

B 站第 2 页的 rel=next 指回自身。遍历识别到环后立即终止，不重复请求该页；扫描标记 `paging_loop=true` / `complete=false`，并声明目录集合不完整。

**扫描清单**

### scan 12 — sim_b

- complete: `False` / paging_loop: `True` / stop_reason: `paging_loop: page http://127.0.0.1:8812/feed?page=2&sort=asc&view=list (page 2) links next to already visited directory http://127.0.0.1:8812/feed?page=2&sort=asc&view=list`
- 页面数: 2，详情请求: 4，实际 HTTP 请求: 11，未发请求即拒绝: 1
- robots: `200`（规则子集见 README，摘录 2 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8812/feed` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |
| 2 | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` | ok | `http://127.0.0.1:8812/feed?page=2&sort=asc&view=list` |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | not_modified | 304 | B-2001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8812/n/grant-round?a=1&b=2` | not_modified | 304 | B-2002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` | not_modified | 304 | B-2003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` | redirect_blocked | None | — | — | — |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/escape` — origin_not_allowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/internal/ops/B-2003` — robots_disallowed (detail link on http://127.0.0.1:8812/feed)
- `http://127.0.0.1:8812/n/offsite-hop?a=1&b=2` — redirect_blocked (detail)

**引擎备注**

- detail link on http://127.0.0.1:8812/feed: refused off-origin URL https://example.invalid/escape; no request made
- detail link on http://127.0.0.1:8812/feed: http://127.0.0.1:8812/internal/ops/B-2003 disallowed by robots.txt; no request made
- paging_loop: page http://127.0.0.1:8812/feed?page=2&sort=asc&view=list (page 2) links next to already visited directory http://127.0.0.1:8812/feed?page=2&sort=asc&view=list
- walk terminated at the cycle without refetching; the directory set for this scan is incomplete
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- redirect 302: http://127.0.0.1:8812/n/water-quality?a=1&b=2 -> http://127.0.0.1:8812/archive/water-quality/B-2001?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/grant-round?a=1&b=2 -> http://127.0.0.1:8812/archive/grant-round/B-2002?a=1&b=2 (in-scope target, followed)
- redirect 302: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2 -> http://127.0.0.1:8812/archive/ethics-guidance/B-2003?a=1&b=2 (in-scope target, followed)
- scan incomplete: no 404 in this scan can count toward withdrawal; documents remain temporarily_unreachable at most

请求清单文件：`reports/requests/scan-12.json`

**证据**

```json
{
  "pages": [
    {
      "page_no": 1,
      "url": "http://127.0.0.1:8812/feed",
      "outcome": "ok",
      "next": [
        "http://127.0.0.1:8812/feed?page=2&sort=asc&view=list"
      ],
      "details": [
        "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
        "http://127.0.0.1:8812/n/grant-round?a=1&b=2",
        "http://127.0.0.1:8812/n/water-quality?a=1&b=2"
      ]
    },
    {
      "page_no": 2,
      "url": "http://127.0.0.1:8812/feed?page=2&sort=asc&view=list",
      "outcome": "ok",
      "next": [
        "http://127.0.0.1:8812/feed?page=2&sort=asc&view=list"
      ],
      "details": [
        "http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2",
        "http://127.0.0.1:8812/n/offsite-hop?a=1&b=2"
      ]
    }
  ],
  "stop_reason": "paging_loop: page http://127.0.0.1:8812/feed?page=2&sort=asc&view=list (page 2) links next to already visited directory http://127.0.0.1:8812/feed?page=2&sort=asc&view=list",
  "complete": false
}
```

## 步骤 9 · 第八轮扫描：临时 503

A-1055 返回 503。该扫描 `complete=false`；503 不推进 404 缺失计数，不生成版本。A-1055 文档状态保持原样（无缺失计数）。

**扫描清单**

### scan 13 — sim_a

- complete: `False` / paging_loop: `False` / stop_reason: `detail_http_503`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | server_error | 503 | — | — | — |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full: HTTP 503; scan will be marked incomplete (503/transient errors do not advance the withdrawal counter)
- scan incomplete: no 404 in this scan can count toward withdrawal; documents remain temporarily_unreachable at most

请求清单文件：`reports/requests/scan-13.json`

**证据**

```json
{
  "stop_reason": "detail_http_503",
  "details": [
    {
      "url": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full",
      "outcome": "server_error",
      "http_status": 503,
      "document_key": null,
      "version_created": false,
      "version_no": 0,
      "version_reason": "",
      "redirected_to": null,
      "canonical_url": null,
      "canonical_blocked": null,
      "raw_digest": "74e1209164947eda50230af14280c0f42c45ffbc788247cc3076b348eec0bbde",
      "previous_raw_digest": null,
      "raw_bytes_changed": false,
      "note": "HTTP 503; not counted as a miss, no version written. This scan is incomplete and does not count as a round",
      "diff": null
    }
  ],
  "document_state": {
    "source_id": "sim_a",
    "document_key": "A-1055",
    "state": "active",
    "retracted_at": null,
    "retracted_scan": null,
    "missing_streak": 0,
    "previous_state": null,
    "last_url_key": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
  }
}
```

## 步骤 10 · 第九轮扫描：首次 404 → 只标记暂不可达

A-1044 首次 404，仅 `temporarily_unreachable`，缺失计数 1，不撤稿；该扫描完整，因此可作为证据轮次。

**扫描清单**

### scan 14 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_found | 404 | A-1044 | — | — |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | not_modified | 304 | A-1055 | 沿用 v1 | http_304_reused_version |

**撤稿裁决**

```json
[
  {
    "document_key": "A-1044",
    "current_scan_id": 14,
    "previous_complete_scan_id": 9,
    "skipped_incomplete_scans": [
      11,
      13
    ],
    "current_misses": [
      "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full"
    ],
    "previous_complete_misses": [],
    "certified": false,
    "reason": "the most recent complete round did not observe a 404 for this document"
  }
]
```

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- document A-1044: 404 observed but withdrawal NOT certified (the most recent complete round did not observe a 404 for this document); remains temporarily_unreachable

请求清单文件：`reports/requests/scan-14.json`

**证据**

```json
{
  "document_state": {
    "source_id": "sim_a",
    "document_key": "A-1044",
    "state": "temporarily_unreachable",
    "retracted_at": null,
    "retracted_scan": null,
    "missing_streak": 1,
    "previous_state": "active",
    "last_url_key": "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full"
  },
  "detail": [
    {
      "url": "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full",
      "outcome": "not_found",
      "http_status": 404,
      "document_key": "A-1044",
      "version_created": false,
      "version_no": 0,
      "version_reason": "",
      "redirected_to": null,
      "canonical_url": null,
      "canonical_blocked": null,
      "raw_digest": "d7ba4f4eb62c4b494a9e5fe83d86189263909c294943bbea7014dfa7b946adbb",
      "previous_raw_digest": null,
      "raw_bytes_changed": false,
      "note": "HTTP 404; URL miss counter 1. Remains temporarily_unreachable until two consecutive complete scans observe 404",
      "diff": null
    }
  ]
}
```

## 步骤 11 · 第十轮扫描：撤稿证据链的干扰项

A-1044 恢复（缺失计数清零、状态回到 active）；A-1055 首次 404。说明只有相邻两轮完整扫描的 404 才构成撤稿证据。

**扫描清单**

### scan 15 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | not_found | 404 | A-1055 | — | — |

**撤稿裁决**

```json
[
  {
    "document_key": "A-1055",
    "current_scan_id": 15,
    "previous_complete_scan_id": 14,
    "skipped_incomplete_scans": [
      11,
      13
    ],
    "current_misses": [
      "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
    ],
    "previous_complete_misses": [],
    "certified": false,
    "reason": "the most recent complete round did not observe a 404 for this document"
  }
]
```

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- document A-1055: 404 observed but withdrawal NOT certified (the most recent complete round did not observe a 404 for this document); remains temporarily_unreachable

请求清单文件：`reports/requests/scan-15.json`

**证据**

```json
{
  "A-1044": {
    "source_id": "sim_a",
    "document_key": "A-1044",
    "state": "active",
    "retracted_at": null,
    "retracted_scan": null,
    "missing_streak": 0,
    "previous_state": "active",
    "last_url_key": "http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full"
  },
  "A-1055": {
    "source_id": "sim_a",
    "document_key": "A-1055",
    "state": "temporarily_unreachable",
    "retracted_at": null,
    "retracted_scan": null,
    "missing_streak": 1,
    "previous_state": "active",
    "last_url_key": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
  }
}
```

## 步骤 12 · 第十一轮扫描：连续两轮完整扫描 404 → 撤稿

A-1055 在第八轮（完整）与本轮（完整）均 404，两轮相邻且都完整，标记 `with_retracted`。历史版本保留可读，撤稿是状态变化而非删除。

**扫描清单**

### scan 16 — sim_a

- complete: `True` / paging_loop: `False` / stop_reason: `completed`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | not_found | 404 | A-1055 | — | — |

**撤稿裁决**

```json
[
  {
    "document_key": "A-1055",
    "current_scan_id": 16,
    "previous_complete_scan_id": 15,
    "skipped_incomplete_scans": [
      11,
      13
    ],
    "current_misses": [
      "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
    ],
    "previous_complete_misses": [
      "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
    ],
    "certified": true,
    "archived_versions_preserved": 1,
    "evidence": "two consecutive complete rounds (15, 16) both observed HTTP 404"
  }
]
```

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- document A-1055: RETRACTED — 404 in consecutive complete rounds 15 and 16; 1 archived version(s) remain readable

请求清单文件：`reports/requests/scan-16.json`

**证据**

```json
{
  "A-1055": {
    "source_id": "sim_a",
    "document_key": "A-1055",
    "state": "with_retracted",
    "retracted_at": "2026-11-12T12:52:14+00:00",
    "retracted_scan": 16,
    "missing_streak": 2,
    "previous_state": "active",
    "last_url_key": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
  },
  "verdicts": [
    {
      "document_key": "A-1055",
      "current_scan_id": 16,
      "previous_complete_scan_id": 15,
      "skipped_incomplete_scans": [
        11,
        13
      ],
      "current_misses": [
        "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
      ],
      "previous_complete_misses": [
        "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
      ],
      "certified": true,
      "archived_versions_preserved": 1,
      "evidence": "two consecutive complete rounds (15, 16) both observed HTTP 404"
    }
  ]
}
```

## 步骤 13 · 第十二轮扫描：503 不构成轮次

A-1055 返回 503，扫描不完整。即便此前已有撤稿证据，这一轮也不能作为新的确认轮；A-1044 的同轮缺失计数同样不被推进。

**扫描清单**

### scan 17 — sim_a

- complete: `False` / paging_loop: `False` / stop_reason: `detail_http_503`
- 页面数: 3，详情请求: 6，实际 HTTP 请求: 10，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 1 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |
| 1 | `http://127.0.0.1:8811/notices` | ok | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` |
| 2 | `http://127.0.0.1:8811/notices/page/2?sort=asc&view=list` | ok | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` |
| 3 | `http://127.0.0.1:8811/notices/page/3?sort=asc&view=list` | ok | — |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | not_modified | 304 | A-1001 | 沿用 v2 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` | not_modified | 304 | A-1002 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | not_modified | 304 | A-1002P | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` | not_modified | 304 | A-1003 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` | not_modified | 304 | A-1044 | 沿用 v1 | http_304_reused_version |
| `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` | server_error | 503 | — | — | — |

**未发请求即被拒绝的候选 URL**

- `https://example.invalid/offsite` — origin_not_allowed (detail link on http://127.0.0.1:8811/notices)
- `http://127.0.0.1:8811/notices/draft-notes/D-77` — robots_disallowed (detail link on http://127.0.0.1:8811/notices)

**引擎备注**

- detail link on http://127.0.0.1:8811/notices: refused off-origin URL https://example.invalid/offsite; no request made
- detail link on http://127.0.0.1:8811/notices: http://127.0.0.1:8811/notices/draft-notes/D-77 disallowed by robots.txt; no request made
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full
- http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full: HTTP 503; scan will be marked incomplete (503/transient errors do not advance the withdrawal counter)
- scan incomplete: no 404 in this scan can count toward withdrawal; documents remain temporarily_unreachable at most

请求清单文件：`reports/requests/scan-17.json`

**证据**

```json
{
  "scan": {
    "complete": false,
    "stop_reason": "detail_http_503",
    "retraction_verdicts": []
  },
  "A-1055": {
    "source_id": "sim_a",
    "document_key": "A-1055",
    "state": "with_retracted",
    "retracted_at": "2026-11-12T12:52:14+00:00",
    "retracted_scan": 16,
    "missing_streak": 2,
    "previous_state": "active",
    "last_url_key": "http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full"
  }
}
```

## 步骤 14 · 第十三轮扫描：中断后重启接续

B 站扫描在请求完 2 个详情后被强制中断；frontier 中保留 pending 行。随后用全新引擎实例重启，检测到 pending URL 并完成剩余请求，最终扫描 complete=true 且不再遗留 pending。

**扫描清单**

### scan 18 — sim_b

- complete: `False` / paging_loop: `False` / stop_reason: `interrupted`
- 页面数: 0，详情请求: 0，实际 HTTP 请求: 0，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 0 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |

**引擎备注**

- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/flaky?a=1&b=2

请求清单文件：``

### scan 19 — sim_b

- complete: `True` / paging_loop: `False` / stop_reason: `completed (resumed)`
- 页面数: 0，详情请求: 0，实际 HTTP 请求: 0，未发请求即拒绝: 0
- robots: `200`（规则子集见 README，摘录 0 组）

| 页面 | URL | 结果 | next |
| --- | --- | --- | --- |

| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |
| --- | --- | --- | --- | --- | --- |

**引擎备注**

- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/water-quality?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/grant-round?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/offsite-hop?a=1&b=2
- resumed pending URL left by an interrupted scan: http://127.0.0.1:8812/n/flaky?a=1&b=2

请求清单文件：``

**证据**

```json
{
  "interrupted_scan_id": 18,
  "interrupted_status": "abandoned",
  "pending_after_interrupt": [
    "http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2",
    "http://127.0.0.1:8812/n/offsite-hop?a=1&b=2",
    "http://127.0.0.1:8812/n/flaky?a=1&b=2"
  ],
  "resumed_scan_id": 19,
  "resumed_urls": [
    "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
    "http://127.0.0.1:8812/n/grant-round?a=1&b=2",
    "http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2",
    "http://127.0.0.1:8812/n/offsite-hop?a=1&b=2",
    "http://127.0.0.1:8812/n/flaky?a=1&b=2"
  ],
  "resumed_complete": true,
  "pending_after_resume": [
    "http://127.0.0.1:8812/n/offsite-hop?a=1&b=2"
  ],
  "documents_versions": {
    "B-2001": 2,
    "B-2002": 1,
    "B-2003": 1,
    "B-2005": 1
  }
}
```

## 附录 A · 版本历史

| source | document | v | scan | issued | title | raw digest | business digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sim_a | A-1001 | 1 | 1 | 2026-03-02 | 关于启动公共数据共享试点的通告 | `c790ae5b23aa…` | `92480a940eea…` |
| sim_a | A-1001 | 2 | 7 | 2026-03-02 | 关于启动公共数据共享试点的通告 | `44f1226d4966…` | `05307f5e6c30…` |
| sim_a | A-1002 | 1 | 1 | 2026-02-18 | 关于发布年度研究报告的通知 | `a8a9bde5f829…` | `861714db1b4b…` |
| sim_a | A-1002P | 1 | 1 | 2026-02-18 | 关于发布年度研究报告（公众版）的通知 | `56da89badfc9…` | `eacbe41d1887…` |
| sim_a | A-1003 | 1 | 1 | 2026-01-22 | 关于开展研究方法培训的通告 | `11876cdecc46…` | `ebf3f4468d6e…` |
| sim_a | A-1044 | 1 | 1 | 2026-01-08 | 关于废止某临时性统计口径的通告 | `865db7feaabb…` | `e494abc7f389…` |
| sim_a | A-1055 | 1 | 1 | 2026-01-03 | 关于维护期间暂停服务的通告 | `bda2bbbffe84…` | `0a23dd9d4c0e…` |
| sim_b | B-2001 | 1 | 2 | 2026-03-11 | Water quality monitoring programme notice | `5a51be24b44d…` | `c2002913a84e…` |
| sim_b | B-2001 | 2 | 8 | 2026-03-11 | Water quality monitoring programme notice (revised) | `f3c247be8855…` | `757857dcb49b…` |
| sim_b | B-2002 | 1 | 2 | 2026-02-25 | Research grant round 2 open call | `f48ee071152a…` | `506407b88134…` |
| sim_b | B-2003 | 1 | 2 | 2026-02-09 | Updated ethics guidance for field studies | `30647a947bd0…` | `ab3c020beb98…` |
| sim_b | B-2005 | 1 | 2 | 2026-01-14 | Notice under maintenance | `a08972ffc6ce…` | `5a5fc9d4e302…` |

## 附录 B · 文档状态

| source | document | state | retracted_at | missing_streak | last URL |
| --- | --- | --- | --- | --- | --- |
| sim_a | A-1001 | active | — | 0 | `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` |
| sim_a | A-1002 | active | — | 0 | `http://127.0.0.1:8811/notices/public-edition/A-1002?sort=asc&view=full` |
| sim_a | A-1002P | active | — | 0 | `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` |
| sim_a | A-1003 | active | — | 0 | `http://127.0.0.1:8811/notices/crossed-canonical/A-1003?sort=asc&view=full` |
| sim_a | A-1044 | active | — | 0 | `http://127.0.0.1:8811/notices/retractable/A-1044?sort=asc&view=full` |
| sim_a | A-1055 | with_retracted | 2026-11-12T12:52:14+00:00 | 2 | `http://127.0.0.1:8811/notices/temporary/A-1055?sort=asc&view=full` |
| sim_b | B-2001 | active | — | 0 | `http://127.0.0.1:8812/n/water-quality?a=1&b=2` |
| sim_b | B-2002 | active | — | 0 | `http://127.0.0.1:8812/n/grant-round?a=1&b=2` |
| sim_b | B-2003 | active | — | 0 | `http://127.0.0.1:8812/n/ethics-guidance?a=1&b=2` |
| sim_b | B-2005 | active | — | 0 | `http://127.0.0.1:8812/n/flaky?a=1&b=2` |

## 附录 C · URL 归一化示例

| 原始 URL | 归一化后（frontier 键） |
| --- | --- |
| `http://127.0.0.1:8811/notices/same-url/A-1001?view=full&sort=asc&utm_source=x&utm_medium=y#section-2` | `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` |
| `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` | `http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full` |
| `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` | `http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public` |
| `http://127.0.0.1:8811/notices/public-edition/A-1002` | `http://127.0.0.1:8811/notices/public-edition/A-1002` |
| `http://127.0.0.1:8812/n/water-quality?b=2&a=1&utm_source=feed` | `http://127.0.0.1:8812/n/water-quality?a=1&b=2` |
| `http://127.0.0.1:8812/n/water-quality?a=1&b=2` | `http://127.0.0.1:8812/n/water-quality?a=1&b=2` |

## 附录 D · 目录索引文件

`diffs/` 保存每个版本变更的段落级差异；`requests/` 保存每次扫描的完整请求清单（含未发出请求的拒绝记录）。

