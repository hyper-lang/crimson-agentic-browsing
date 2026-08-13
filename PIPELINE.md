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
        | consumes "confirmed_scams"; runs a browser-use agent (Playwright +
        | Gemini) per domain: signs up, navigates to the deposit/wallet
        | section, reports any wallet addresses found
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
matched anything. Filled in verbatim from the paper's Appendix D, Table 11:
- `keyword_in_url` = Invest Words ∪ Coin Words (36 + 58 terms, the paper's
  "URL-filter" column grouping)
- `recv.py`'s three lists = Invest Words, Coin Words, Context Words
  separately, matching `OCR()`'s existing three-way intersection check.

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

One thing that turned out *not* to be a gap, contrary to an earlier version
of this document: `screenshot_retrier()` already constructs a brand new
`Service` + `webdriver.Chrome` on every single call and tears both down in
`finally` — so a crashed or stale browser session self-heals on the very
next attempt. There's no persistent-driver resilience issue here; §5 in an
earlier draft of this document was wrong about that.

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
- Per-worker named volumes (`recv1-data`, `recv1-logs`, `recv1-results`,
  same for `recv2`) so `SYSNO=1` and `SYSNO=2` don't collide on disk.
- `crimson-validate` now depends on `rabbitmq` being healthy — it had no
  such dependency before, since it never touched RabbitMQ at all.
- Chromium + `chromium-driver` installed via apt directly in the shared
  image (§3.13) — there is no separate Selenium container.
- `crimson-crawler` builds from its own `x-crimson-crawler-build` anchor,
  a separate Python 3.12 + Playwright image — it doesn't share
  `crimson-python-env` with the other five services (§3.18).

### 3.18 `crawler.py`: stage 6, now implemented (basic version)

Modeled directly on
[hyper-lang/crimson_browsing](https://github.com/hyper-lang/crimson_browsing)'s
`scrape_wallets.py` — an agentic approach using
[`browser-use`](https://github.com/browser-use/browser-use) (Playwright
under the hood, not Selenium) rather than hand-scripted Selenium steps like
`authentication_crawling/crawler_script.py` (the original repo's approach,
left untouched — you're rewriting that logic here, in `crawler.py`, instead
of extending it directly).

Consumes `confirmed_scams`, same pattern as every other stage. Per domain,
gives an LLM agent (Gemini by default) a natural-language task: sign up
with placeholder identity info, navigate to the deposit/wallet section,
report any wallet addresses found — structured into a typed
`SignupResult` (Pydantic) rather than free text. Output is appended to
`results/wallet_extraction.jsonl`.

Deliberately basic, per your request — this sets up the environment and
the queue wiring; the actual task prompt, retry behavior, structured
output schema, and concurrency are all things to experiment with once it's
running. A few things worth knowing about the current state:

- **Requires a real `GOOGLE_API_KEY` in `.env`** (Gemini). Nothing runs
  without one — this isn't optional the way most other env vars in this
  project are.
- **`headless=False` in the reference script becomes `headless=True`
  here.** The original's whole point of a visible browser was letting a
  human intervene for CAPTCHA/email verification/KYC. There's no display
  in this container, so right now the agent just reports those cases via
  its `notes` field instead of solving them. Interactive fallback would
  need Xvfb/VNC infrastructure added later if you want it.
- **`prefetch_count=1`** — one browser session running at a time. Each
  message is a full agentic browsing session against a real site, not
  cheap to run in parallel yet. Scale the same way as `recv.py`
  (`crimson-recv-1`/`-2`): duplicate the service block once you've
  validated it works reliably on a handful of domains.
- Uses a **separate build** from the other five services
  (`x-crimson-crawler-build`, Python 3.12) rather than sharing
  `crimson-python-env` — `browser-use` requires Python ≥3.12 and Playwright
  manages its own browser install (`playwright install --with-deps
  chromium`), a different stack from the apt-installed Selenium/Chromium
  the rest of the pipeline uses.

`url` is the only field of the `confirmed_scams` message the crawler
currently uses; `ioc` may already contain wallet addresses `iocsearcher`
found sitting in plain HTML without requiring login at all — worth
checking that field before assuming every domain needs the full
sign-up/login flow. `reason`, `ip_info`, and `title` ride along in the
message but aren't used by `crawler.py` yet.

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
- **`crawler.py` has no dedup at all.** Unlike every other stage, nothing
  stops the same domain being crawled twice if it somehow enters
  `confirmed_scams` more than once. Given each run is a real, possibly
  detectable sign-up attempt against a live scam site, this is worth
  fixing before running it unattended for any length of time.
- **`crawler.py` has no interactive fallback.** `headless=True` means
  CAPTCHA/email-verification/KYC walls just get reported in `notes`
  instead of handled — expect a meaningful fraction of domains to dead-end
  there rather than yield a wallet address.
- **`crawler.py` result validation is minimal.** `wallet_addresses_found`
  is whatever the LLM agent claims it saw, unverified — no format
  checking against the address regexes `iocsearcher` already uses
  elsewhere in this pipeline. Worth cross-checking before treating the
  output as ground truth.

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
