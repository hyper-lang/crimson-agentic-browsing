# Crimson with Agentic Browsing

A reimplementation of [Crimson](https://github.com/pragseclab/Crimson) — the
cryptocurrency-investment-scam detection pipeline from the WWW'25 paper *"The
Poorest Man in Babylon: A Longitudinal Study of Cryptocurrency Investment
Scams"* — with its wallet-extraction stage replaced by an **agentic browser**.

The paper's authors noted that automated wallet extraction had a *low success
rate* and that they fell back to **navigating scam sites by hand** to recover
deposit addresses (paper §2.4). This repo automates that step: an LLM-driven
browser signs up, reaches the deposit page, and extracts the wallet addresses.

## Contribution

Stage 6 (wallet extraction) runs **wallet_browser** (`Crimson/vendor/wallet_browser`):
a [Scrapling](https://github.com/D4Vinci/Scrapling) stealth browser (patchright
Chromium + a Cloudflare solver) driven step-by-step by **Claude through the
Claude Code CLI** — no per-site scripting, and it handles signup forms,
multi-step deposit flows, and image CAPTCHAs across unseen scam-site templates.

The rest of the pipeline follows the paper, with these operational changes:
- Dockerized services (RabbitMQ, certstream, and the Python stages)
- [certstream-server-rust](https://github.com/reloading01/certstream-server-rust) in place of the original Elixir server
- [wordninja-enhanced](https://github.com/timminator/wordninja-enhanced) in place of wordninja
- Raw HTML persisted to disk alongside screenshots
- Hardcoded hosts moved to a `.env` file
- URL and content-filter keyword lists filled in from the paper's Table 11
- Bounded RabbitMQ queues and a `data/` retention job so the single-box
  deployment doesn't fill its disk (see [PIPELINE.md](PIPELINE.md))

Designed and architected by Zane Wong; Claude (via Claude Code) was used to
assist development.

## Pipeline

Six stages (paper §2); see [PIPELINE.md](PIPELINE.md) for the full architecture
and every deviation from the original.

```
1. certstream-server      Certificate Transparency logs -> new TLS cert domains
        |  (listen.py -> "urls" queue)
2. Domain filter           send.py: split the domain into words (wordninja) +
   (send.py)               stem, keep it if it hits a URL keyword; 12h buffer
        |  ("cryptoscams" queue)
3. Content selection       recv.py: availability check -> full-page screenshot
   (recv.py x6)            (Selenium/Chromium) -> OCR (tesseract) + HTML text ->
                           keep only if the text contains an Invest word AND a
                           Coin word AND a Context word (all stemmed)
        |  ("ocr_results" queue)
4. LLM classification      validate.py: Ollama (llama3:70b) decides whether the
   (validate.py)           page is a crypto investment scam -> "yes"
        |  ("confirmed_scams" queue)
5. Agentic wallet          crawler.py -> wallet_browser: sign up, reach the
   extraction (crawler.py) deposit page, extract the deposit wallet addresses
        |
6. Output                  results/crawler/wallet_extraction.jsonl
```

## Requirements

- **Linux with Docker Engine.** Every service uses `network_mode: host`, which
  Docker Desktop on macOS/Windows does not support the same way.
- **An Ollama server reachable from the box, serving `llama3:70b`** (stage 4).
  It can run on the same machine or another; set its host in `.env`.
- **A Claude Code subscription token** for stage 6. Generate one with
  `claude setup-token` and put it in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
  Without it the crawler exits on startup.

## Setup

```bash
git clone https://github.com/hyper-lang/crimson-agentic-browsing
cd crimson-agentic-browsing
cp .env.example .env
```

Edit `.env` and set at least:
- `CLAUDE_CODE_OAUTH_TOKEN=` — from `claude setup-token`
- `OLLAMA_HOST` / `OLLAMA_PORT` — your Ollama server (defaults to localhost:11434)

Every other value defaults to `localhost` and only needs changing for a
multi-machine deployment. See `.env.example` for the full list.

## Run

```bash
docker compose build
docker compose up -d
```

Watch progress at the RabbitMQ management UI (`http://localhost:15672`,
guest/guest): `urls` should drain, `cryptoscams` fill and process, and
`confirmed_scams` feed the crawler.

- Screenshots + HTML of candidate sites: `data/recvN/`
- Confirmed scams: `results/validate/confirmed_scams_YYMM.jsonl`
- Extracted wallets: `results/crawler/wallet_extraction.jsonl`
  (**note:** this file contains the throwaway account passwords the crawler
  registers with — treat it as sensitive)

## Future work

- Ablating models and prompts to improve extraction rate
- Automating email verification (currently a dead-end for email-gated signups)
- Enumerating scam domains with [LOKI](https://arxiv.org/abs/2509.12181)
- An ML scam classifier (e.g. [LightGBM](https://github.com/lightgbm-org/LightGBM)) in place of, or before, the LLM stage

## License

This project is a derivative of [Crimson](https://github.com/pragseclab/Crimson),
which is licensed under **GPL-3.0**. As a derivative work it is also released
under **GPL-3.0** — see [LICENSE](LICENSE). Modified pipeline scripts under
`Crimson/` retain the original authors' copyright.
