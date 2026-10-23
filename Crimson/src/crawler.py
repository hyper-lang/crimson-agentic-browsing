import functools
import json
import logging
import os
import sys
import threading
import time

import pika
from wallet_browser.crawler import crawl

os.makedirs('results', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[logging.FileHandler('results/crawler.log'), logging.StreamHandler()],
)
# Scrapling has its own stderr handler; don't print its lines twice.
logging.getLogger('scrapling').propagate = False
logging.getLogger('pika').setLevel(logging.WARNING)

RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'localhost')
SOURCE_QUEUE_NAME = 'confirmed_scams'  # populated by validate.py
RESULTS_FILE = 'results/wallet_extraction.jsonl'
# Every domain that got a result line, so a redelivered message doesn't
# register a second account on the same scam site.
DONE_FILE = 'results/crawled.txt'

# Stage 6 is wallet_browser (vendor/wallet_browser): Scrapling's stealth
# Chromium driven by Claude through the Claude Code CLI. Same settings and
# defaults as its own .env.
MODEL = os.environ.get('WALLET_BROWSER_MODEL', 'sonnet')
HEADLESS = os.environ.get('WALLET_BROWSER_HEADLESS', 'true').lower() != 'false'
CRAWL_TIMEOUT = int(os.environ.get('WALLET_BROWSER_TIMEOUT', '600'))
SOLVE_CF = os.environ.get('WALLET_BROWSER_SOLVE_CLOUDFLARE', 'true').lower() != 'false'
SOLVE_CAPTCHA = os.environ.get('WALLET_BROWSER_SOLVE_CAPTCHA', 'true').lower() != 'false'

# One crawl at a time, even if a reconnect delivers a new message while the
# previous crawl's thread is still running.
crawl_lock = threading.Lock()


def load_done():
    try:
        with open(DONE_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


done_domains = load_done()


def crawl_domain(domain):
    with crawl_lock:
        if domain in done_domains:
            logging.info(f"Already crawled {domain}, skipping")
            return
        output = crawl(domain, model=MODEL, headless=HEADLESS, solve_cf=SOLVE_CF,
                       solve_captcha=SOLVE_CAPTCHA, timeout=CRAWL_TIMEOUT)
        # Contains the throwaway account's password -- see wallet_browser README.
        with open(RESULTS_FILE, 'a') as f:
            f.write(json.dumps(output) + '\n')
        # A crash (e.g. the Claude CLI failing) isn't a result for the site;
        # leave it uncrawled so it can be re-sent later.
        if output['notes'].startswith('Crawl exception:'):
            return
        with open(DONE_FILE, 'a') as f:
            f.write(domain + '\n')
        done_domains.add(domain)


def ack(ch, delivery_tag):
    # The connection may have dropped mid-crawl; the message is then
    # redelivered and skipped via done_domains.
    if ch.is_open:
        ch.basic_ack(delivery_tag=delivery_tag)


def work(connection, ch, delivery_tag, body):
    try:
        record = json.loads(body)
        domain = record.get('url')
        if domain:
            crawl_domain(domain)
        else:
            logging.error(f"Message missing url, skipping: {record}")
    except Exception as e:
        logging.error(f"Crawl failed for {body[:200]!r}: {e}")
    connection.add_callback_threadsafe(functools.partial(ack, ch, delivery_tag))


def callback(ch, method, properties, body, connection):
    # A crawl takes minutes. Running it inside this callback blocked pika's
    # I/O loop, so heartbeats stopped, RabbitMQ dropped the connection, the
    # ack failed and the same domain was redelivered and crawled again
    # forever. Crawl on a thread instead and ack from the I/O loop when done;
    # prefetch_count=1 still keeps it to one message at a time.
    threading.Thread(target=work, args=(connection, ch, method.delivery_tag, body),
                     daemon=True).start()


def main():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
            channel = connection.channel()
            channel.queue_declare(queue=SOURCE_QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=SOURCE_QUEUE_NAME,
                                  on_message_callback=functools.partial(callback, connection=connection))
            logging.info(f"Consuming from '{SOURCE_QUEUE_NAME}'.")
            channel.start_consuming()
        except pika.exceptions.AMQPError as e:
            logging.error(f"RabbitMQ connection error, reconnecting: {e}")
            time.sleep(10)


if __name__ == '__main__':
    # Without it every crawl fails, so don't drain the queue: exit and let
    # compose's restart policy retry until the token is set.
    if not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'):
        sys.exit("CLAUDE_CODE_OAUTH_TOKEN is not set (generate one with `claude setup-token`)")
    main()
