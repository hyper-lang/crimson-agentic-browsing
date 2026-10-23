# wallet-browser

A command-line tool that signs up on crypto scam sites and extracts the deposit
/ wallet addresses they hand out. The browser is
**[Scrapling](https://github.com/D4Vinci/Scrapling)**'s `AsyncStealthySession`
(patchright Chromium with fingerprint hardening and a Cloudflare
Turnstile/interstitial solver). Each step is driven by **Claude through the
local Claude Code CLI**, so no API key is needed.

```bash
wallet-browser scamsite.com
```

Code (`src/wallet_browser/`):

- `cli.py`: the `wallet-browser` command (flags, `.env` defaults, output)
- `crawler.py`: the signup → deposit → extract flow, scope guard, bot-wall handling
- `page_agent.py`: LLM `act()` / `extract()` over the Playwright page
- `claude_cli.py`: locked-down `claude -p` wrapper

## What it does

- Opens each site in a stealth session on patchright's bundled Chromium. In
  testing, headless real Google Chrome was blocked by Cloudflare's interstitial
  every time while the bundled build passed, so `CHROME_PATH` is only an override.
- Keeps top-level navigation on the target domain and its subdomains, and
  blocks every request to localhost/private/link-local addresses (scope/SSRF
  guard). Sub-resources and iframes from other domains (CDNs, the Cloudflare
  challenge frame) are still allowed.
- Also stops if an HTTP redirect carries the page off the target domain, and
  retries over `http://` when a bare-domain target's HTTPS is broken.
- **Cloudflare:** when it detects a full-page challenge (on landing, on the way
  to signup, or after submitting), or an embedded Turnstile widget on a form, it
  runs Scrapling's Cloudflare solver. Hard firewall blocks ("Attention
  Required", 403) and Cloudflare's "Suspected Phishing" page are recorded, not
  solved. `--no-solve-cloudflare` / `WALLET_BROWSER_SOLVE_CLOUDFLARE=false` records
  every wall as `BLOCKED` without trying.
- **Image / math CAPTCHAs on signup and login forms:** screenshots of
  CAPTCHA-looking images are attached to the Claude call that fills the form,
  and a rejected submit is retried up to 2 more times with the fresh image.
  reCAPTCHA / hCaptcha are not solved (`BLOCKED: inline CAPTCHA`).
  `--no-solve-captcha` / `WALLET_BROWSER_SOLVE_CAPTCHA=false` turns this off.
- **Deposit addresses:** after signup it opens the "new deposit" form, reads
  which crypto payment options it lists, and for each one picks it, enters the
  minimum amount and continues to the payment-details page (never confirming a
  payment), then extracts the address. Each address must appear verbatim in the
  page source and is reported with the source's casing, so CSS upper-casing or
  a misread can't corrupt the results.

## How the Claude part works

Scrapling supplies the browser but has no LLM primitives, so `page_agent.py`
implements the two the flow needs:

- `act(instruction)`: tags every visible interactive element with an id and
  sends Claude a compact list of them plus the visible text. Claude returns an
  ordered list of `click` / `fill` / `select` / `check` actions (JSON schema
  via `claude -p --json-schema`), and Playwright performs them, falling back
  to DOM clicks/fills when an element is covered or off-screen. One CLI call
  per `act()`, so a whole signup form is one call. `act_steps()` repeats it on
  each new page when a step navigates (e.g. coin → amount page → continue).
- `extract(instruction, Model)`: sends the visible text plus input values,
  copy-button payloads and image alt text (where deposit addresses usually
  hide), and parses the answer into a pydantic model.

Every CLI call is locked down (no tools, no MCP, no settings/hooks, no skills,
empty cwd) because the prompt carries untrusted page content. The model can
only answer, never act on this machine. Usage counts against your subscription.

## Install

Build a wheel here, copy it to the machine that will run the crawls, and
install it there. Nothing needs installing on the build machine beyond `uv`.

```bash
# on this machine
uv build --wheel                 # -> dist/wallet_browser-0.1.0-py3-none-any.whl
scp dist/wallet_browser-0.1.0-py3-none-any.whl user@crawler-box:

# on the crawler machine (needs Python 3.12+ and Claude Code logged in: `claude`)
uv tool install ./wallet_browser-0.1.0-py3-none-any.whl   # or: pipx install ./wallet_browser-*.whl
wallet-browser --install-browser                          # one-time: patched Chromium
wallet-browser example.com -o -
```

To upgrade, rebuild and run `uv tool install --force ./wallet_browser-*.whl`
there. To run from a source checkout without installing anything:
`uv run wallet-browser ...` in this folder.

Settings can also go in a `.env` in the directory you run from (see
`.env.example`); real environment variables and flags take precedence.

## Usage

```bash
# One or more sites (bare domains get https://, falling back to http:// if TLS is broken)
wallet-browser scamsite.com https://other.site

# From a text file (one target per line; blank lines and # comments skipped)
wallet-browser -f targets.txt

# Just print results, don't append to results/wallet_extraction.jsonl
wallet-browser scamsite.com -o -

# Only results on stdout, pipe-friendly
wallet-browser -q -f targets.txt -o - | jq -r '.wallet_addresses_found[]'
```

Sites are crawled one at a time.

### Docker (recommended for suspected-malicious sites)

The container can't see your terminal login, so give it its own token:

```bash
cp .env.example .env
claude setup-token         # then set CLAUDE_CODE_OAUTH_TOKEN=... in .env
./run.sh scamsite.com -o -
./run.sh -f targets.txt    # same arguments as wallet-browser
```

`run.sh` builds the image (patchright Chromium + Claude CLI + wallet-browser)
and runs it on a bridge network with all capabilities dropped and
no-new-privileges. Results land in `./results/`.

## Output

One JSON object per site, printed to stdout and appended to
`results/wallet_extraction.jsonl` under the current directory (`-o FILE` to
change, `-o -` to disable).
Logs go to stderr (`-q` for warnings only), so stdout can be piped into `jq`:

```json
{"url": "lendex.me", "signed_up": true, "final_url": "...",
 "wallet_addresses_found": ["TCtYhAxgdCKLwQ8BHJJmpCMdGohK5TmRn2", "bc1qy2n0k048d9rdmtvwvv764uvz8rhlgzwvcs0x75"],
 "account": {"username": "jordanlee123456", "email": "jordan.lee.123456@example.com",
             "password": "Secure@Pass123456!", "name": "Jordan Lee", "phone": "+15559..."},
 "notes": "Signed up and reached deposit area; found 6 address(es)."}
```

`account` is the throwaway persona the crawler registered with (a fresh one
per site and run), so you can log back in later and tie each address to the
account it was issued to. It is recorded even when the crawl times out or
fails after signup. The results file therefore contains working passwords for
those accounts; treat it accordingly.

`notes` starts with one of these when the crawl stops early:

| Prefix | Meaning |
|---|---|
| `BLOCKED:` | Bot wall the solver couldn't clear, a hard WAF block, or an inline CAPTCHA on the signup form |
| `FLAGGED:` | Cloudflare's "Suspected Phishing" warning; Cloudflare has already flagged the site |
| `No account signup found:` | Brochure/contact-form site with no registration |
| `Left target domain` | An HTTP redirect went off-domain; the crawl stopped there |
| `Signup incomplete (...)` | Email verification, SMS/KYC, or another blocker after submitting |
| `REFUSED:` | Claude's safety classifier declined a step; the site was stopped there (no retry), keeping any addresses already found |

A refusal means that site is a known gap in the data, not a real "no signup" or
"no wallets" result. Count them with
`jq -r 'select(.notes|startswith("REFUSED:")) | .url' results/wallet_extraction.jsonl`.

Find blocked sites:

```bash
jq -r 'select(.notes|startswith("BLOCKED:")) | .url' results/wallet_extraction.jsonl
```

## Options

| Flag | Env var (`.env`) | Default |
|---|---|---|
| `-f`, `--file TXT` | | |
| `-o`, `--output FILE` | | `results/wallet_extraction.jsonl` |
| `--timeout SECS` | `WALLET_BROWSER_TIMEOUT` | 600 per site |
| `--model NAME` | `WALLET_BROWSER_MODEL` | `sonnet` |
| `--headed` | `WALLET_BROWSER_HEADLESS=false` | headless |
| `--no-solve-cloudflare` | `WALLET_BROWSER_SOLVE_CLOUDFLARE=false` | solver on |
| `--no-solve-captcha` | `WALLET_BROWSER_SOLVE_CAPTCHA=false` | solving on |
| `--profile DIR` | | fresh temp profile |
| `-q`, `--quiet` | | info logging |
| `--install-browser` | | |
| | `WALLET_BROWSER_STEP_TIMEOUT` | 240s per Claude call |
| | `CHROME_PATH` | patchright's bundled Chromium |
| | `CLAUDE_BIN` | `claude` |

`--profile DIR` reuses a browser profile's cookies/session across runs (e.g.
log in once in a `--headed` run, then reuse the session, including a
`cf_clearance` cookie).

## Notes / limitations

- The signup → deposit → extract flow is scripted in `_run_flow()`. Adjust the
  `act()`/`extract()` steps there for sites with unusual layouts.
- `act()` sees only the top-level document. Forms inside iframes (some embedded
  signup widgets) aren't reachable yet.
- Each `act()`/`extract()` is a separate `claude -p` process (~5–30s). A site
  with a dozen payment options takes ~5 minutes; raise `--timeout` if sites
  get cut off. Each payment option visited may leave a pending (unpaid) deposit
  on the throwaway account.
- Cloudflare solving calls Scrapling's private `_cloudflare_solver()`, so
  Scrapling is pinned in `pyproject.toml`; re-check that call when upgrading.
