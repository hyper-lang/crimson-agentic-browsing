import asyncio
import json
import logging
import os
import time

import pika
from browser_use import Agent, Browser, ChatGoogle
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[logging.FileHandler('crawler.log'), logging.StreamHandler()],
)

RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'localhost')
SOURCE_QUEUE_NAME = 'confirmed_scams'  # populated by validate.py
RESULTS_FILE = 'results/wallet_extraction.jsonl'

# Modeled on https://github.com/hyper-lang/crimson_browsing/blob/main/scrape_wallets.py
MODEL = os.environ.get('CRIMSON_CRAWLER_MODEL', 'gemini-2.5-flash')
FALLBACK_MODEL = os.environ.get('CRIMSON_CRAWLER_FALLBACK_MODEL', 'gemini-3-flash-preview')
# headless=False in the reference script so a human can step in for
# CAPTCHA/email verification/KYC. There's no display in this container, so
# it defaults to headless here -- the agent will just report those cases
# via `notes` instead of solving them. Revisit once you're ready to
# experiment with a VNC/Xvfb setup for interactive fallback.
HEADLESS = os.environ.get('CRIMSON_CRAWLER_HEADLESS', 'true').lower() != 'false'


class SignupResult(BaseModel):
    signed_up: bool
    final_url: str
    wallet_addresses_found: list[str]
    notes: str


async def signup_and_find_wallet(url: str) -> SignupResult:
    from urllib.parse import urlparse
    target_host = urlparse(url).hostname or url
    browser = Browser(
        headless=HEADLESS,
        # Confines navigation to the target domain (and its subdomains) --
        # without this, a malicious page could redirect the agent anywhere,
        # including sites entirely unrelated to the one being investigated.
        allowed_domains=[target_host, f"*.{target_host}"],
        # block_ip_addresses defaults to True in browser-use -- keep it
        # that way. It blocks direct-IP navigation and localhost/internal
        # hostnames even when an allowlist is set, which matters here:
        # a scam page could otherwise try to redirect the agent at your
        # own infrastructure (SSRF-style) rather than just other domains.
        # Never set this False for this use case.
        block_ip_addresses=True,
        # Scam sites frequently have broken/self-signed TLS. Accepting
        # that is a real tradeoff (no cert validation at all for this
        # session) made deliberately so navigation isn't blocked outright --
        # revisit if that tradeoff doesn't sit right for your threat model.
        ignore_https_errors=True,
        # disable_security=False (the default) is never overridden here --
        # this keeps browser-use's own origin/security checks active.
        # No user_data_dir is set, so each call gets a fresh, ephemeral
        # profile -- no cookies, history, or storage persist between runs
        # or leak from one scam site's session into the next one's.
    )
    agent = Agent(
        task=(
            f"Go to {url}. Sign up for a new account using "
            f"the name 'Jordan Lee', email 'jordan.lee.test+{{random}}@example.com', "
            f"and a strong password. Use placeholder values for any other required "
            f"fields, skip optional ones. If email verification is required, pause "
            f"and report that instead of trying to access the email account. "
            f"After signup, navigate to the deposit, wallet, or 'receive crypto' "
            f"section of the account dashboard. Report any wallet/deposit addresses "
            f"shown there for the logged-in account (e.g. BTC, ETH, USDT addresses)."
        ),
        llm=ChatGoogle(model=MODEL),
        fallback_llm=ChatGoogle(model=FALLBACK_MODEL),
        browser=browser,
        output_model_schema=SignupResult,
    )
    try:
        history = await agent.run()
        result: SignupResult | None = history.structured_output
        if result is None:
            # agent.run() completed without ever producing a structured
            # result -- happens when it exhausts retries (e.g. every LLM
            # call failing, a site the agent couldn't make progress on,
            # etc). Previously this crashed with AttributeError on
            # result.signed_up below, which meant the message still got
            # acked (see callback()) but nothing was ever written to
            # wallet_extraction.jsonl -- a failed crawl vanished with zero
            # record instead of being visible as a failure.
            result = SignupResult(
                signed_up=False,
                final_url=url,
                wallet_addresses_found=[],
                notes="Agent did not produce a structured result -- likely exhausted retries or failed outright.",
            )
    finally:
        await browser.close()
    return result


def callback(ch, method, properties, body):
    try:
        record = json.loads(body)
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode message from {SOURCE_QUEUE_NAME}: {e}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    domain = record.get('url')
    if not domain:
        logging.error(f"Message missing url, skipping: {record}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    logging.info(f"Crawling {domain}")
    start_time = time.time()
    try:
        result = asyncio.run(signup_and_find_wallet("http://" + domain))
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
            f"Finished {domain} in {time.time() - start_time:.2f}s -- "
            f"wallets found: {len(result.wallet_addresses_found)}"
        )
    except Exception as e:
        logging.error(f"Crawl failed for {domain}: {e}")
        # Was log-only -- a failure here previously meant the message
        # still got acked (never retried) but nothing was ever written to
        # wallet_extraction.jsonl, so a failed crawl vanished with no
        # trace beyond ephemeral docker logs.
        os.makedirs('results', exist_ok=True)
        with open(RESULTS_FILE, 'a') as f:
            f.write(json.dumps({
                "url": domain,
                "signed_up": False,
                "final_url": None,
                "wallet_addresses_found": [],
                "notes": f"Crawl failed with an exception: {e}",
            }) + '\n')
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
            channel = connection.channel()
            channel.queue_declare(queue=SOURCE_QUEUE_NAME, durable=True)
            # One browser session at a time to start -- each is a full
            # agentic run against a real site, not cheap. Bump this (and
            # add more crawler instances, same pattern as crimson-recv-*)
            # once you've seen it work reliably on a handful of domains.
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=SOURCE_QUEUE_NAME, on_message_callback=callback)
            logging.info(f"Consuming from '{SOURCE_QUEUE_NAME}'.")
            channel.start_consuming()
        except pika.exceptions.StreamLostError as e:
            logging.error(f"RabbitMQ connection lost, reconnecting: {e}")
            time.sleep(10)
        except pika.exceptions.AMQPConnectionError as e:
            logging.error(f"RabbitMQ connection error, retrying: {e}")
            time.sleep(10)


if __name__ == '__main__':
    main()
