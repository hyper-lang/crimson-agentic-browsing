import asyncio
import json
import logging
import os
import random
import time

import pika
from browser_use import Agent, Browser, ChatGoogle
from browser_use.browser.profile import BrowserProfile
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[logging.FileHandler('crawler.log'), logging.StreamHandler()],
)

RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'localhost')
SOURCE_QUEUE_NAME = 'confirmed_scams'
RESULTS_FILE = 'results/wallet_extraction.jsonl'
MODEL = os.environ.get('CRIMSON_CRAWLER_MODEL', 'gemini-2.5-flash')
FALLBACK_MODEL = os.environ.get('CRIMSON_CRAWLER_FALLBACK_MODEL', 'gemini-3-flash-preview')
HEADLESS = os.environ.get('CRIMSON_CRAWLER_HEADLESS', 'true').lower() != 'false'
# Per-site crawl budget: 5 minutes is generous for most scam sites
CRAWL_TIMEOUT = int(os.environ.get('CRIMSON_CRAWLER_TIMEOUT', '300'))


class SignupResult(BaseModel):
    signed_up: bool
    final_url: str
    wallet_addresses_found: list[str]
    notes: str


def _build_task(url: str) -> str:
    suffix = random.randint(100000, 999999)
    phone_mid = random.randint(100, 999)
    phone_end = random.randint(1000, 9999)
    return f"""
You are a security researcher investigating a potential cryptocurrency investment scam at {url}.
Your job: create an account, then extract every cryptocurrency deposit/wallet address the site reveals.

=== STEP 1 — REACH THE SIGNUP PAGE ===
Navigate to {url}. If the landing page is not a signup form, look for a button or link labeled
"Sign Up", "Register", "Join", "Open Account", "Get Started", or similar, and click it.
The registration URL often contains: /register, /signup, /join, /open-account.

=== STEP 2 — COMPLETE SIGNUP ===
Fill the form with:
  Name:      Jordan Lee
  Email:     jordan.lee.{suffix}@example.com
  Username:  jordanlee{suffix}   (use if a separate username field exists)
  Password:  Secure@Pass{suffix}!
  Phone:     +15559{phone_mid}{phone_end}  (use if required; try skipping optional phone fields)
  Birthday:  1990-01-15  (use if required)
  Country:   United States  (pick from dropdown if required)
  Any other required field: use a believable placeholder value.

Check all required checkboxes (terms of service, etc.). Submit the form.

=== STEP 3 — HANDLE POST-SIGNUP ===
After submitting:
- If you land on a dashboard or account page → proceed to Step 4.
- If the site redirects to a LOGIN page → log in with the credentials you just used.
- If EMAIL VERIFICATION is required and you cannot complete it → record signed_up=False,
  note "email verification required", and stop.
- If SMS / KYC verification blocks further access → record signed_up=False, note the blocker,
  and stop.

=== STEP 4 — FIND ALL DEPOSIT / WALLET ADDRESSES ===
This is the critical step. Systematically explore the authenticated dashboard:

1. Scan the navigation for items like: Deposit, Add Funds, Wallet, Assets, Portfolio, Recharge,
   Receive, Transfer In, Fund Account, Top Up, My Wallet. Click each one you find.

2. On the deposit page, the site typically lets you choose a cryptocurrency. Cycle through
   every option offered — at minimum try: BTC, ETH, USDT (ERC-20), USDT (TRC-20), BNB, USDC,
   TRX. For each one, click/select it and record any wallet address that appears.

3. If the deposit page has a form requiring an amount before showing the address, enter 1000.

4. Wallet addresses appear as long alphanumeric strings. Formats to recognise:
   - BTC:       starts with 1, 3, or bc1
   - ETH/ERC20: starts with 0x (42 chars)
   - USDT-TRC20/TRX: starts with T (34 chars)
   - BNB/BSC:   starts with 0x or bnb1
   - LTC:       starts with L or M
   - XRP:       starts with r
   Look for them in visible text, input fields, copy-buttons, and QR code labels/alt-text.

5. If there are sub-tabs (e.g. "On-chain deposit" vs "Internal transfer"), check all tabs.

6. Do NOT navigate away from the target domain.

Collect every unique address found. If you found zero addresses after a genuine attempt,
explain in notes what you encountered (e.g. "site showed only fiat deposit options",
"deposit section required additional KYC", etc.).
"""


async def signup_and_find_wallet(url: str) -> SignupResult:
    from urllib.parse import urlparse
    target_host = urlparse(url).hostname or url

    # Fix: block_ip_addresses lives in BrowserProfile, not BrowserSession.
    # ignore_https_errors does not exist in this API; disable_security is the equivalent.
    browser = Browser(
        headless=HEADLESS,
        allowed_domains=[target_host, f"*.{target_host}"],
        disable_security=True,
        browser_profile=BrowserProfile(
            block_ip_addresses=True,
        ),
    )
    agent = Agent(
        task=_build_task(url),
        llm=ChatGoogle(model=MODEL),
        fallback_llm=ChatGoogle(model=FALLBACK_MODEL),
        browser=browser,
        output_model_schema=SignupResult,
    )
    try:
        history = await asyncio.wait_for(agent.run(), timeout=CRAWL_TIMEOUT)
        result: SignupResult | None = history.structured_output
        if result is None:
            result = SignupResult(
                signed_up=False,
                final_url=url,
                wallet_addresses_found=[],
                notes="Agent completed without producing a structured result.",
            )
    except asyncio.TimeoutError:
        result = SignupResult(
            signed_up=False,
            final_url=url,
            wallet_addresses_found=[],
            notes=f"Crawl timed out after {CRAWL_TIMEOUT}s.",
        )
    finally:
        await browser.close()
    return result


def _try_url_schemes(domain: str) -> str:
    """Return https:// URL if the domain likely supports it, else http://."""
    # Prefer https; the agent has disable_security=True so bad certs won't block it.
    return "https://" + domain


def callback(ch, method, properties, body):
    try:
        record = json.loads(body)
    except json.JSONDecodeError as e:
        logging.error(f"Bad message from {SOURCE_QUEUE_NAME}: {e}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    domain = record.get('url')
    if not domain:
        logging.error(f"Message missing url, skipping: {record}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    url = _try_url_schemes(domain)
    logging.info(f"Crawling {url}")
    start_time = time.time()
    try:
        result = asyncio.run(signup_and_find_wallet(url))
        output = {
            "url": domain,
            "signed_up": result.signed_up,
            "final_url": result.final_url,
            "wallet_addresses_found": result.wallet_addresses_found,
            "notes": result.notes,
        }
        os.makedirs('results', exist_ok=True)
        with open(RESULTS_FILE, 'a') as f:
            f.write(json.dumps(output) + '\n')
        logging.info(
            f"Finished {domain} in {time.time() - start_time:.2f}s — "
            f"wallets: {len(result.wallet_addresses_found)}"
        )
    except Exception as e:
        logging.error(f"Crawl failed for {domain}: {e}")
        os.makedirs('results', exist_ok=True)
        with open(RESULTS_FILE, 'a') as f:
            f.write(json.dumps({
                "url": domain,
                "signed_up": False,
                "final_url": None,
                "wallet_addresses_found": [],
                "notes": f"Crawl exception: {e}",
            }) + '\n')
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
            channel = connection.channel()
            channel.queue_declare(queue=SOURCE_QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=SOURCE_QUEUE_NAME, on_message_callback=callback)
            logging.info(f"Consuming from '{SOURCE_QUEUE_NAME}'.")
            channel.start_consuming()
        except pika.exceptions.StreamLostError as e:
            logging.error(f"RabbitMQ stream lost, reconnecting: {e}")
            time.sleep(10)
        except pika.exceptions.AMQPConnectionError as e:
            logging.error(f"RabbitMQ connection error, retrying: {e}")
            time.sleep(10)


if __name__ == '__main__':
    main()
