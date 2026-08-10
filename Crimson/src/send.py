import os
import pika
import json
import logging
import pytz
from datetime import datetime
from collections import OrderedDict
from logging.handlers import TimedRotatingFileHandler
import time
import utils.keyword_utils as kw_utils
from cachetools import LRUCache

# Configuration
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'localhost')
QUEUE_NAME = 'cryptoscams'
# Paper §2.1.3: a 12-hour buffer between a domain entering the queue and a
# worker picking it up, to give slow-to-deploy sites time to go live.
# Implemented as a RabbitMQ dead-letter delay: filtered domains are published
# to DELAY_QUEUE_NAME with a per-message TTL; once the TTL expires, RabbitMQ
# automatically dead-letters the message into QUEUE_NAME, which recv.py
# consumes unchanged. No plugin required, no change needed in recv.py.
DELAY_QUEUE_NAME = 'cryptoscams_delay'
QUEUE_DELAY_MS = int(os.environ.get('CRIMSON_QUEUE_DELAY_MS', str(12 * 60 * 60 * 1000)))
LOG_DIR = "sender-logs"
CACHE_CAPACITY = 50000

# CACHE_CAPACITY was defined but never used in the original script -- CT logs
# reissue/re-log the same certificate (and therefore the same domain names)
# repeatedly, so without dedup every repeat re-runs wordninja/tldextract and
# re-enters the 12h delay queue as a brand new entry.
seen_domains = LRUCache(maxsize=CACHE_CAPACITY)

# Setup logging
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "send.log")
logger = logging.getLogger("SendLogger")
logger.setLevel(logging.DEBUG)
handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
handler.suffix = "%Y-%m-%d"
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
formatter.converter = time.gmtime
handler.setFormatter(formatter)
logger.addHandler(handler)

# Timezone configuration
newYorkTz = pytz.timezone("America/New_York")

def parse_domain_name(domain_name):
    return domain_name[2:] if domain_name.startswith('*') else domain_name

def log_domains(url_name, curr_date, filename, mode='a'):
    log_path = os.path.join("logs", curr_date)
    os.makedirs(log_path, exist_ok=True)
    with open(os.path.join(log_path, filename), mode) as f:
        f.write(f'{url_name}\n')

def enqueue_domains(message, context, channel):
    if message['message_type'] != "certificate_update":
        return
    all_domains = message['data']['leaf_cert']['all_domains']
    curr_date = str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:]
    for each_domain in all_domains:
        url_name = parse_domain_name(each_domain.lower())
        log_domains(url_name, curr_date, 'all_domains_seen.txt', 'a')
        if url_name in seen_domains:
            continue
        seen_domains[url_name] = True
        if not kw_utils.match_domain_name_with_keywords(url_name):
            log_domains(url_name, curr_date, 'failed_url_filter.txt', 'a')
            continue
        log_domains(url_name, curr_date, 'passed_url_filter.txt', 'a')
        
        channel.basic_publish(
            exchange='',
            routing_key=DELAY_QUEUE_NAME,
            body=url_name,
            properties=pika.BasicProperties(delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE)
        )
        log_domains(f" [x] Sent {url_name}", curr_date, 'sent.txt', 'a')

# --- Domain Selection stage (paper §2.1.2) ---
# Consumes the raw certstream messages listen.py publishes to the "urls"
# queue, applies the keyword filter above, and republishes the domains
# that pass into "cryptoscams" -- the paper's Central Domain Queue that
# recv.py's worker nodes actually drain.
SOURCE_QUEUE_NAME = 'urls'

def callback(ch, method, properties, body):
    try:
        # listen.py does body=json.dumps(message) where `message` is
        # ALREADY the raw JSON text from the certstream websocket, so
        # this is double-encoded -- undo both layers here.
        raw_text = json.loads(body)
        message = json.loads(raw_text)
        enqueue_domains(message, None, ch)
    except Exception as e:
        logger.error(f"Error processing message from {SOURCE_QUEUE_NAME}: {e}")
    finally:
        ch.basic_ack(delivery_tag=method.delivery_tag)

def main():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
            channel = connection.channel()
            channel.queue_declare(queue=SOURCE_QUEUE_NAME, durable=True)
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.queue_declare(
                queue=DELAY_QUEUE_NAME,
                durable=True,
                arguments={
                    'x-message-ttl': QUEUE_DELAY_MS,
                    'x-dead-letter-exchange': '',
                    'x-dead-letter-routing-key': QUEUE_NAME,
                }
            )
            channel.basic_qos(prefetch_count=50)
            channel.basic_consume(queue=SOURCE_QUEUE_NAME, on_message_callback=callback)
            logger.info("Consuming from '%s', publishing filtered domains to '%s'.", SOURCE_QUEUE_NAME, QUEUE_NAME)
            channel.start_consuming()
        except pika.exceptions.StreamLostError as e:
            logger.error(f"RabbitMQ connection lost, reconnecting: {e}")
            time.sleep(10)
        except pika.exceptions.AMQPConnectionError as e:
            logger.error(f"RabbitMQ connection error, retrying: {e}")
            time.sleep(10)

if __name__ == '__main__':
    main()
