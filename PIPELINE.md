# Crimson Pipeline — Dockerized Fork: Architecture & Changes

This documents how the Dockerized version of Crimson actually works end-to-end,
and everywhere it now differs from the original WWW'25 paper repo
("The Poorest Man in Babylon"). It's written to be read alongside the source,
not as a replacement for it.

## 1. Pipeline overview

Six stages, per paper §2:

```
Certificate Transparency logs
        |
        v
 [1] certstream-server  (container: certstream-server)
        | raw certstream JSON, over websocket
        v
 [2] listen.py           (container: crimson-listener)
        | publishes each raw cert message, JSON-encoded, to queue "urls"
        v
        RabbitMQ queue: urls
        |
        v
 [3] send.py             (container: crimson-sender)      -- Domain Selection
        | consumes "urls"; splits each SAN via wordninja-enhanced,
        | stems + checks against the Invest+Coin keyword set
        | (paper Table 11, "URL-filter")
        | passing domains -> queue "cryptoscams_delay" (12h TTL)
        v
        RabbitMQ queue: cryptoscams_delay --(TTL expiry, dead-letter)--> cryptoscams
        |
        v
 [4] recv.py             (containers: crimson-recv-1, crimson-recv-2)  -- Content-based Selection
        | consumes "cryptoscams"; per domain:
        |   - HTTP availability check
        |   - full-page screenshot (Chromium + chromedriver, installed
        |     directly inside the same container -- see §3.13)
        |   - OCR + HTML text, stemmed, checked against Invest+Coin+Context
        |     word lists (paper Table 11, all three columns)
        | on a match -> queue "ocr_results"
        v
        RabbitMQ queue: ocr_results
        |
        v
 [5] validate.py          (container: crimson-validate)   -- LLM-assisted Classification
        | consumes "ocr_results"; classifies via Ollama (llama3:70b)
        | answer "yes" -> queue "confirmed_scams"
        v
        RabbitMQ queue: confirmed_scams
        |
        v
 [6] crawler.py           (container: crimson-crawler)  -- Account Creation & Wallet Extraction
        | consumes "confirmed_scams"; runs wallet_browser (Scrapling stealth
        | Chromium + Claude Code CLI) per domain: signs up, opens the
        | deposit form per coin, extracts source-verified addresses
        v
        results/wallet_extraction.jsonl
```

Everything through stage 6 is a long-running Docker service that stays up
indefinitely (`restart: unless-stopped`), consuming its input queue as
messages arrive.

## 2. Queue topology

| Queue | Producer | Consumer | Durable | Notes |
|---|---|---|---|---|
| `urls` | `listen.py` | `send.py` | yes | Raw certstream messages, double-JSON-encoded (see §3.4) |
| `cryptoscams_delay` | `send.py` | *(none — TTL only)* | yes | Per-message TTL = `CRIMSON_QUEUE_DELAY_MS` (default 12h); dead-letters into `cryptoscams` on expiry |
| `cryptoscams` | RabbitMQ (dead-letter) | `recv.py` | yes | The paper's "Central Domain Queue" |
| `ocr_results` | `recv.py` | `validate.py` | yes | One message per positive OCR match; JSON body is the same `log_data` dict `recv.py` writes to its local `results.log` |
| `confirmed_scams` | `validate.py` | `crawler.py` | yes | One message per LLM-confirmed scam |

All five services connect to the same RabbitMQ instance via `RABBITMQ_HOST`
(default `localhost`, working because everything runs under
`network_mode: host`).

## 3. Everything that differs from the original repo

### 3.1 Environment-configurable hosts (was: hardcoded IPs / `'localhost'` literals)
- `recv.py`: `QUEUE_IP` (used for both the RabbitMQ host and the rsync
  target — same as the original design) is now `CRIMSON_HOST`, default
  `localhost`.
- `send.py`, `listen.py`, `validate.py`: `RABBITMQ_HOST` env var, default
  `localhost`.
- `listen.py`: certstream endpoint is now `CERTSTREAM_HOST`/`CERTSTREAM_PORT`
  instead of a hardcoded IP. **The port also changed**: the original pointed
  at `4000` (the old CaliDog certstream server's port); the
  `certstream-server-rust` image used here serves on `8080`. This was a
  real bug, not just an inflexibility — the websocket connection would have
  hung indefinitely against the wrong port.
- `validate.py`: `OLLAMA_HOST`/`OLLAMA_PORT`/`OLLAMA_MODEL`, default
  `localhost:11434`, `llama3:70b`.

### 3.2 `send.py`: went from non-functional to Domain Selection
The original script defined `enqueue_domains()` but had no code that ever
called it — no consumer, no `main()`. It would start, log, and exit having
done nothing. It's now a persistent consumer on `urls` (see queue topology
above), reconnecting on `StreamLostError`/`AMQPConnectionError` the same way
`recv.py` does.

### 3.3 Keyword lists were empty placeholders
`keyword_utils.py`'s `keyword_in_url` and `recv.py`'s `invest_words` /
`coin_words` / `context_words` were all empty (`# Update as needed!`),
meaning the domain filter rejected everything and the content filter never
matched anything. Filled in from the paper's Appendix D, Table 11, which
has four columns: URL-filter, then the Content-Filter's Invest, Coin and
Context Words.
- `keyword_in_url` = the URL-filter column (the 36 keywords of §2.1.2).
- `recv.py`'s three lists = Invest Words (just "invest"), Coin Words,
  Context Words, matching `OCR()`'s three-way intersection check.

An earlier version misread the table: Invest Words held the URL-filter
column, "invest" sat at the head of Coin Words, and `keyword_in_url` was
URL-filter + Coin Words. Since "invest" counted as a coin word, any page
that mentioned investing passed without saying anything about crypto.
Replaying September's 8,919 OCR positives (OCR text only), the corrected
lists pass 580 instead of 4,837, and 279 of the 304 "confirmed scams" no
longer pass at all.

### 3.4 `listen.py` double-JSON-encoding
`listen.py` publishes `body=json.dumps(message)`, where `message` is
*already* the raw JSON string from the certstream websocket — so the queue
body is JSON-encoded twice. `send.py`'s consumer un-wraps both layers
(`json.loads` twice) before touching the payload. This was true in the
original code too; it's not a bug we introduced, just one that had to be
handled correctly once a real consumer existed.

### 3.5 Stemming (paper §2.1.2, §2.2)
The paper stems words before comparison (e.g. "investors"/"investing" →
"invest") in both the domain-based and content-based filters. Neither
`keyword_utils.py` nor `recv.py`'s `OCR()` did this. Added `nltk`'s
`PorterStemmer` to both — keyword lists are stemmed once at import time,
input tokens are stemmed per call.

### 3.6 12-hour queue buffer (paper §2.1.3)
The paper buffers a domain for 12 hours between queueing and processing, to
give slow-to-deploy scam sites time to actually go live. Not implemented
anywhere in the original code. Implemented via a RabbitMQ dead-letter delay
queue entirely inside `send.py` — filtered domains publish to
`cryptoscams_delay` (per-message TTL = `CRIMSON_QUEUE_DELAY_MS`, default
12h), which dead-letters into `cryptoscams` on expiry. `recv.py` needed no
changes at all; it keeps consuming `cryptoscams` exactly as before.

### 3.7 `validate.py`: batch CSV job → queue consumer
Original: a one-shot script reading `data/ocr_results_{month}.csv`, a file
nothing else in the pipeline produced (`recv.py` writes structured JSON to
`results/{SYSNO}/results.log.<date>`, a different format and location
entirely — the original script's input was disconnected from the rest of
the codebase). Also shelled out to a local `ollama` binary via
`subprocess`, and had a `month = ""` variable requiring manual editing per
run.

Now: a persistent RabbitMQ consumer on `ocr_results`, calling Ollama's HTTP
API directly (`OLLAMA_HOST`/`PORT`/`MODEL`), computing `month` dynamically
per message (`%y%m` in America/New_York, matching the other scripts'
date convention) instead of requiring a hand-edit. `scams_{month}.txt` /
`not_scams_{month}.txt` / `errors_{month}.txt` / `done.txt` are all still
written in the same format as before, so anything downstream that depended
on those files still works. On a "yes" classification, also publishes to
`confirmed_scams` for the crawler.

The system prompt is the paper's Appendix B prompt. The repo's version only
described the JSON output format and never stated the question, so "yes"
drifted to meaning "legitimate": the confirmed-scam log was full of
reasons like "transparency" and "credibility".

One deliberate simplification: the original prompt structure was
`[system, user(empty), assistant("Sure, I will now assess..."), user(text)]`
— a priming pattern suited to the CLI-driven `ollama run` invocation. The
HTTP chat API doesn't need that scaffolding, so it's just
`[system, user(text)]` now.

### 3.8 `recv.py`: rsync target was a literal placeholder
Both `sync()` calls passed the literal string `'add_central_server_path'` as
the remote path — not a real path, and both calls used the *same* string, so
`results/{SYSNO}/` and `data/{SYSNO}/check/` (two unrelated local
directories) would have rsynced into the identical remote directory. Split
into `CRIMSON_REMOTE_RESULTS_PATH` and `CRIMSON_REMOTE_CHECK_PATH` (both
env-configurable, sensible defaults). Also: the check/ sync used to fire
unconditionally for every domain, even though `data/{SYSNO}/check/` is only
ever populated on an actual positive match — every non-match was silently
failing an rsync of a directory that didn't exist (logged to
`rsync-failure.txt`, harmless but noisy). Now only synced when
`check()` returns `"Scam Found!"`.

### 3.9 `send.py`: dead dedup config, now implemented
`CACHE_CAPACITY = 50000` was defined but never used anywhere. Given CT logs
constantly reissue/re-log the same certificates (and therefore the same SAN
domain names), this looks like an unfinished dedup feature. Added an
`LRUCache` (`seen_domains`, `maxsize=CACHE_CAPACITY`) so a domain already
seen isn't re-run through wordninja/tldextract or re-published into the
delay queue on every repeat sighting. The `all_domains_seen.txt` audit log
still records every sighting, duplicates included — only the
filter+publish step is skipped.

### 3.10 `listen.py`: no recovery from a mid-run RabbitMQ drop
The websocket side had reconnect logic (`on_close` → `start_websocket_listener()`),
but if the RabbitMQ connection dropped after startup, `on_message`'s
`try/except` just logged the error and kept discarding every subsequent
message forever — the process would look perfectly healthy in
`docker compose ps` while silently producing nothing. `on_message` now calls
`setup_rabbitmq_connection()` again on a publish failure.

### 3.11 Dependency additions
`wordninja` → `wordninja-enhanced` (your change, to add crypto-specific
words without the missing custom language model file). `tldextract`,
`nltk`, and `wordninja-enhanced` itself were never in any requirements file
and had to be added. The stray `logging` and misordered `psutil` entries
that appeared in an intermediate version of `requirements.txt` were removed
— `logging` is stdlib and the PyPI package of that name doesn't install
under Python 3 at all.

### 3.13 `utils/screenshot.py`: no remote Selenium Grid, hardcoded local Chrome path
`SeleniumScreenshot` never used a remote WebDriver — it launches Chrome
*locally, inside whatever process runs it*, via
`chrome_options.binary_location`, hardcoded to
`/home/ubuntu/cryptoscams/datacollection/testing/chrome-unpacked/chrome-linux64/chrome`
(a manually unpacked Chrome build specific to the original authors' own
VM) and `chromedriver` at `/usr/bin/chromedriver`. An earlier version of
this Dockerized fork incorrectly assumed a remote Selenium Grid container
was needed and added one (`selenium-chrome`, `selenium/standalone-chrome`)
— that container was never actually used by any code in this repo and has
been removed. Chromium + `chromium-driver` are now installed directly in
the shared image (apt keeps the two version-matched automatically, which
is more robust than the original's manually-paired binary+driver). Both
paths are now `CRIMSON_CHROME_BINARY` / `CRIMSON_CHROMEDRIVER_PATH` env
vars, defaulting to the apt-installed locations
(`/usr/bin/chromium`, `/usr/bin/chromedriver`).

`screenshot_retrier()` builds a fresh `Service` + `webdriver.Chrome` per
attempt and tears both down in `finally`, so a crashed browser session
self-heals on the next attempt. It used to leak, though: it called
`service.start()` itself before passing the service to
`webdriver.Chrome()`, which starts it again. In Selenium 4 every
`Service.start()` spawns a new chromedriver, so the first one was orphaned
on every attempt (up to two per domain). After a day each recv worker had
~1,000 stray chromedrivers and 2-3 GiB of memory, the host ran out of
memory, and the OOM killer took recv workers and `certstream-server`. The
manual `start()` is gone; a worker now holds no chromedriver between
domains.

### 3.15 Raw HTML now persisted to disk
Previously, the HTML fetched by `is_domain_available()` only ever existed
in memory during OCR/IOC extraction — nothing wrote it anywhere. `check()`
now saves it as `page.html` alongside `full_page.png` in
`data/{SYSNO}/screenshots/{curr_date}/{domain}/`, sharing that directory's
existing lifecycle exactly: deleted on a screenshot or OCR failure (same
`rm -rf` that already ran), copied into `data/{SYSNO}/check/{curr_date}/`
on an actual positive match (`handlePositives`' existing `cp -r` picks it
up automatically, no change needed there).

### 3.16 `confirmed_scams` records now persisted, not just queued
`validate.py`'s `scams_{month}.txt` only ever recorded the LLM's
`answer`/`reason` — the richer context (`ioc`, `ip_info`, `title`) only
existed inside the RabbitMQ message published to `confirmed_scams`. Once a
crawler consumes and acks that message, that data would be gone for good.
Every confirmed record is now also appended as one JSON line to
`results/confirmed_scams_{month}.jsonl` (in the `crimson-validate`
container's `validate-results` volume) immediately before publishing —
this is the durable, independent-of-the-queue record of every domain that
made it to stage 6.

### 3.17 Docker/Compose-specific (not present in the original repo at all)
- `network_mode: host` on every service so `localhost` means the same thing
  everywhere, with every host/port above overridable via `.env` for a
  non-localhost deployment later.
- A shared `x-crimson-build` anchor so every Python service builds from the
  same Dockerfile instead of only one service having a `build:` block (the
  original cause of the very first `pull access denied` errors).
- Six recv workers (`crimson-recv-1` ... `-6`), each with its own bind
  mounts (`./data/recvN`, `./logs/recvN`, `./results/recvN`) so their
  `SYSNO`s don't collide on disk.
- `restart: unless-stopped` on every service, including `rabbitmq` and
  `certstream-server`. certstream had none, so a single OOM kill stopped
  all intake until someone noticed.
- `crimson-validate` now depends on `rabbitmq` being healthy — it had no
  such dependency before, since it never touched RabbitMQ at all.
- Chromium + `chromium-driver` installed via apt directly in the shared
  image (§3.13) — there is no separate Selenium container.
- `crimson-crawler` builds from its own `x-crimson-crawler-build` anchor,
  wallet_browser's image (Python 3.12, Claude Code CLI, patchright
  Chromium) -- it doesn't share `crimson-python-env` (§3.18).

### 3.18 `crawler.py`: stage 6 = wallet_browser

Stage 6 runs [wallet_browser](Crimson/vendor/wallet_browser/README.md),
vendored under `Crimson/vendor/wallet_browser` so the server only needs this
repo: Scrapling's stealth session on patchright Chromium (with a Cloudflare
solver), driven step by step by Claude through the Claude Code CLI. The
`crimson-crawler` image is wallet_browser's own Dockerfile (Python 3.12,
Claude Code CLI, patchright Chromium), and the service keeps run.sh's
lockdown: bridge network, `cap_drop: ALL`, `no-new-privileges`.

`crawler.py` is now only the queue glue: it consumes `confirmed_scams` and
calls `wallet_browser.crawler.crawl()` per domain. Results (including the
throwaway account credentials) go to `results/wallet_extraction.jsonl`;
crawled domains go to `results/crawled.txt` so a redelivered message
doesn't sign up twice.

- **Requires `CLAUDE_CODE_OAUTH_TOKEN` in `.env`** (`claude setup-token`).
  Settings are wallet_browser's `WALLET_BROWSER_*` variables.
- The crawl runs on a worker thread and is acked through
  `add_callback_threadsafe`. The previous browser-use version crawled inside
  the pika callback, which blocked heartbeats for the whole crawl: RabbitMQ
  dropped the connection, the ack failed, and the same domain was
  redelivered and crawled again indefinitely.
- Addresses must appear verbatim in the page source (wallet_browser's
  `verify_addresses`), so they are no longer just whatever the LLM claimed.

## 4. Known gaps not addressed here

These are real, but weren't in scope for this pass — flagging so they don't
get lost:

- **`domain_whitelist` in `keyword_utils.py` is still empty.** Not
  something the paper specifies; it's an operational allowlist you'd
  maintain yourselves.
- **`mkdirs()` in `recv.py` assumes `data/` and `logs/` already exist**
  (`os.listdir('data/')` without a prior existence check) — harmless under
  Docker because the named volumes create their mount points automatically,
  but it would crash on a bare `python src/recv.py` run outside a
  container with no pre-existing directories.
- **`is_domain_available()` swallows all exceptions silently**, with no
  `log()` call on the generic `except` branch (unlike almost every other
  function in `recv.py`), so a domain check failing for a non-HTTP reason
  (DNS failure, connection reset, etc.) leaves no trace anywhere. Possibly
  intentional to reduce log noise given how many domains will simply not
  resolve, but worth a second opinion.
- **`month`-scoped output files** (`scams_{month}.txt` etc.) reset their
  effective dedup granularity at each month boundary via `done.txt`, which
  is *not* month-scoped — a domain marked done in one month is skipped
  forever, even in later months. This matches the original script's
  behavior exactly, just noting it's carried forward rather than fixed.
- **Email verification still dead-ends.** The persona uses `@example.com`
  addresses; the paper used Mailinator inboxes, which can receive the
  verification mail.

## 5. Running it

```
docker compose build
docker compose up -d
```

Verification steps (RabbitMQ UI at `http://localhost:15672`) are the same
as covered earlier in this conversation — watch `urls` drain,
`cryptoscams_delay` fill and (after `CRIMSON_QUEUE_DELAY_MS`) empty into
`cryptoscams`, `ocr_results` receive positive matches, and
`confirmed_scams` receive LLM-confirmed ones. There's no Selenium Grid
console to check anymore (§3.13) — Chrome runs inside `crimson-recv-1`/`-2`
directly, so screenshot failures show up in those containers' own logs.

## 6. Change log

### October 2026: classification fixes, wallet_browser crawler, recv stability

September's run produced 304 "confirmed scams" that were almost all
legitimate sites (wealth managers, charities, a health-funding project),
and the crawler extracted 0 wallets from 313 crawls. Causes and fixes:

| Problem | Effect | Fix | Section |
|---|---|---|---|
| Table 11 columns misread: Invest Words held the URL-filter column and "invest" was listed as a Coin Word | any page saying "invest" passed the content filter without mentioning crypto | lists match Table 11; URL filter is the 36 URL-filter words only | §3.3 |
| validate.py prompt never stated the question | "yes" drifted to mean "legitimate" (reasons: transparency, legitimate, credibility) | paper's Appendix B prompt | §3.7 |
| browser-use crawler | 295/313 crawls ended without a result; crawl inside the pika callback blocked heartbeats, causing redelivery | wallet_browser (vendored), crawl on a worker thread, crawled-domain dedup, refuses to start without `CLAUDE_CODE_OAUTH_TOKEN` | §3.18 |
| chromedriver started twice per screenshot | ~1,000 orphaned chromedrivers per worker, OOM kills | removed manual `service.start()` | §3.13 |
| no restart policy on certstream/rabbitmq | certstream OOM-killed, intake stopped for ~22h | `restart: unless-stopped` | §3.17 |

Measured results:
- Replaying September's 8,919 OCR positives (OCR text only), the corrected
  lists pass 580 instead of 4,837; 279 of the 304 bad confirmations no
  longer pass.
- First two days with the fixes: 56 confirmed / 333 rejected, with reasons
  like "promises" (29), "guarantee", "solicitation", on domains such as
  bitloxy.com, cointradepro.org and growmoney.pro. Remaining false
  positives are mostly meme-coin pages, prop-trading firms and signal
  groups.
- After the leak fix: 0 lingering chromedrivers per worker, host memory
  steady at ~3.4 GiB (was 13 of 15 GiB).

Everything produced before the fix (all of September, `2609`) was made by
the broken filters and should not be used as ground truth. On the server it
was moved out of the live folders into `archive/2609-prefix/`, keeping the
same layout (`results/`, `data/`, `logs/`), so new output never mixes with
it. The browser-use crawler rows are in
`archive/2609-prefix/results/crawler/wallet_extraction_browseruse.jsonl`.
The queues held nothing pre-fix: RabbitMQ's data volume was created with
the fixed deploy on 2026-10-24.
