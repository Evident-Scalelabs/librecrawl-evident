# Private HTTP acquisition service

This fork preserves LibreCrawl's crawl/API owners. Evident owns factor methods,
scoring and client output. Cloudflare owns dynamic rendering and journeys; this
entrypoint refuses JavaScript rendering and PageSpeed work.

## Railway source deployment

Connect this repository with Railpack, Python 3.12, and `python service.py`.
The upstream Dockerfile is named `Dockerfile.upstream` to prevent automatic
Dockerfile builds. No Docker or browser installation is needed.

Set `LIBRECRAWL_API_KEY` to a generated machine bearer secret and `SECRET_KEY` to
a separate stable Flask session signing secret. Set `PORT=5000`. Keep one service
replica: active crawls live in one Python process and SQLite owns durable rows.
Mount a Railway volume at `/app/data` so `data/users.db` survives redeployments.
Use private networking only, with no public domain. `/healthz` checks SQLite;
health includes `producerRevision` from Railway’s source commit identity (null locally).
Waitress listens on both IPv4 and IPv6 for health checks and private networking;
all `/api/*` requests require `Authorization: Bearer <service key>`. UI and
login routes return 404. There is no external identity provider or customer login.
Health also exposes the fixed, non-secret request policy. The HTTP identity is
`LibreCrawl/1.0 (Web Crawler)`. Settings, start and both resume paths refuse a
different identity, robots opt-out, custom headers or proxies. Ambient proxy and
netrc settings are disabled too. A saved crawl with incompatible policy must not
be resumed; create a new bounded request instead.

Robots permission is checked by the shared HTTP transport before page, HEAD,
sitemap and redirect-target dispatch. Fetching robots itself (and its redirect
chain) is exempt from recursive robots checks. HTTP 404/410 permits acquisition;
401/403 denies it. Other HTTP failures, oversized robots and network failures
fail closed with explicit acquisition gaps. `robots_disallowed` and
`robots_unavailable` rows have no page body; neither is a customer Finding or a
complete page capture. No identity rotation, browser impersonation or challenge
evasion is provided by this entrypoint.

The service starts idle. It does not crawl, render, query providers or invoke a
model on startup. Supply no PageSpeed or other provider key.

## Existing HTTP API

Use a separate cookie jar per crawl; the signed session cookie addresses its
in-memory crawler. Keep the bearer secret server-side. Evident remains the
tenant/scan authority and records the returned `crawl_id` under its owning job.

1. `POST /api/save_settings` with explicit acquisition limits, for example
   `{"maxDepth":2,"maxUrls":100,"crawlDelay":1,"concurrency":2,"maxFileSize":2,
   "enableJavaScript":false,"enablePageSpeed":false,"enableDuplicationCheck":false}`.
   Size is in MiB. Start/resume/settings-update admission requires finite positive
   limits: at most 1,000 pages, depth 10 and 10 MiB per decoded body. It rejects
   the upstream five-million-URL/50-MiB UI defaults; save bounded settings first.
2. `POST /api/start_crawl` with `{"url":"https://the-addressed-site/"}`. Save
   `crawl_id` from the response. This step performs acquisition.
3. Poll `GET /api/crawl_status?stats_only=1`; fetch `/api/crawl_status` for the
   full current rows, links and upstream diagnostic issues. Keep errors and
   `link_check_only` distinct from actual fetched pages.
4. `GET /api/crawls/<id>` reloads the persisted rows. `POST
   /api/crawls/<id>/load` loads them for streaming exports. The existing
   `/api/crawls/<id>/resume` path resumes interrupted work explicitly; startup
   recovery only marks interrupted crawls failed and never restarts acquisition.

`GET /api/export_stream?format=json&type=urls&fields=url,final_url,status_code,error_type,body_base64,body_text,body_sha256,response_headers,fetched_at,capture_method`
streams the selected fields. The same fields work with the existing
`POST /api/export_data` JSON export. Request fields explicitly: default exports
are small SEO summaries.

HTTP page rows now retain the response entity bytes as `body_base64`, their
SHA-256 as `body_sha256`, parsed HTML text as `body_text`, response headers,
final URL, UTC acquisition time and `capture_method: http`. Bytes are exactly
`requests.Response.content` after HTTP content decoding, not compressed wire
bytes. HTML text is a derived convenience; the bytes are the replay source.
Every shared-session response, including robots/sitemaps and redirect responses,
is streamed with the decoded-byte cap independently of HEAD or Content-Length.
Compressed sitemap files have a separate bounded gzip inflation step. Ancillary
oversize/parse failures persist an `Acquisition` issue receipt, so an unread
sitemap or robots file is not represented as verified empty. A page body over the cap stops the
read and records `error_type: file_too_large`, its observed HTTP status, headers,
final URL and bytes observed (a lower bound on total size). It has no retained
body/hash/text or content-derived diagnostic issues. Include `error_type` in
exports and never treat this row as complete evidence. Transient stream-read
failures use the existing bounded retry count; oversize captures are not retried.
Headers and bodies are private source evidence and can contain site/session
data. Historical rows and HEAD-only checks have no body evidence; do not infer
it from a successful status code.

Failed SQLite writes keep pending rows for retry and surface `persistence_error`.
Successful batches are removed separately, so retrying failed pages does not
duplicate already committed links/issues. A crawl with a failed final save is
marked failed. Source bodies increase memory/storage use; bounded crawl limits
are required. Resume checkpoints retain upstream's 1,000-pending-URL limit;
this first slice does not promise complete frontier recovery for larger crawls.

The page cap includes active request reservations. Stop/pause stops new dispatch
and drains already dispatched results through the same retention owner. Queued
GET/HEAD/redirect requests check stop again after pacing, before HTTP dispatch. A stop
may return `success: false` while status is `stopping`; the instance cannot be
reused or evicted during drain or after a failed save. SIGTERM closes acquisition
admission, requests stop for every owner, waits for acquired work, and flushes
inactive failed batches too. The process reports failure if final persistence
fails. Cleanup only removes successfully drained and saved instances.

## Offline checks

`python tests/evidence_tests.py` checks exact bytes/text/headers/final URL through
SQLite and JSON exports, retry-safe persistence, shared GET/HEAD/redirect pacing,
decoded-body limits, stream failure/response closure, and private bounded
HTTP-only API admission. The twelve cases include compressed ancillary limits,
active-page reservations, stop/drain and failed-save cleanup/shutdown.
Policy fixtures also refuse spoofed settings/saved resumes and prove denied
HEAD/redirect/sitemap requests and unknown robots never dispatch the page body.
`SKIP_BROWSER_TESTS=1 python
tests/fixture_tests.py` exercises the existing local HTTP fixtures with its
browser case explicitly skipped. These commands use no browser or paid service.

Upstream MIT copyright/permission notices remain in `LICENSE`.
