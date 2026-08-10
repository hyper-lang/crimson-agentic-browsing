import json
import logging
import os
import time

import pika
import pytz
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler('llama.log'),
        logging.StreamHandler(),  # was file-only; docker compose logs showed nothing
    ],
)

# --- RabbitMQ ---
# Was a one-shot batch job reading data/ocr_results_{month}.csv, a file
# nothing else in the pipeline actually produced. recv.py now publishes each
# positive OCR match directly to SOURCE_QUEUE_NAME, and this consumes it
# continuously, same as send.py/recv.py.
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'localhost')
SOURCE_QUEUE_NAME = 'ocr_results'          # populated by recv.py on a positive OCR match
CONFIRMED_QUEUE_NAME = 'confirmed_scams'   # consumed downstream by the crawler

# --- Remote Ollama server ---
OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'localhost')
OLLAMA_PORT = os.environ.get('OLLAMA_PORT', '11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'llama3:70b')
OLLAMA_URL = f'http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat'
REQUEST_TIMEOUT = int(os.environ.get('OLLAMA_TIMEOUT', '120'))

newYorkTz = pytz.timezone("America/New_York")

SYSTEM_PROMPT = (
    'You are a financial advisor programmed to provide responses strictly in JSON format. '
    'Each response must contain keys "answer" and "reason". The answer key can have a value '
    'of either "yes" or "no", and the reason key should have a one word reason. Example: '
    'Input: "Invest now for a guaranteed return of 10 percent in one month." '
    'Output: {"answer": "yes", "reason": "promises"}. Please adhere strictly to this output format.'
)


def load_processed_domains(file_path):
    try:
        with open(file_path, 'r') as file:
            return set(line.strip() for line in file)
    except FileNotFoundError:
        return set()


# Loaded once at startup, same dedup contract as the original script --
# still a single flat file, not scoped per month.
done_domains = load_processed_domains('done.txt')


def current_month():
    # Original script had a blank `month = ""` the operator had to hand-edit.
    # Compute it instead, matching recv.py/send.py's YYMMDD-style convention.
    return datetime.now(newYorkTz).strftime('%y%m')


def call_ollama(messages):
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",  # ask Ollama to constrain output to valid JSON
    }
    response = requests.post(OLLAMA_URL, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def validate_response(response):
    return (
        isinstance(response, dict)
        and 'answer' in response
        and 'reason' in response
        and isinstance(response['answer'], str)
        and isinstance(response['reason'], str)
    )


def run_ollama_request(url_name, base_messages, error_file):
    max_attempts = 5
    attempt = 0
    messages = list(base_messages)

    while attempt < max_attempts:
        attempt += 1
        if attempt > 1:
            logging.info(f"{url_name} Attempt#{attempt}")
        try:
            result = call_ollama(messages)
            content = result.get('message', {}).get('content', '')
            response = json.loads(content)

            if validate_response(response):
                return response

            correction_prompt = "Please provide a response with keys 'answer' (yes/no) and 'reason' (one-word explanation)."
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": correction_prompt},
            ]
            continue

        except json.JSONDecodeError as e:
            logging.error(f'{url_name} -- JSON decoding failed: {e}')
            continue
        except requests.exceptions.RequestException as e:
            logging.error(f'{url_name} -- Ollama request failed: {e}')
            time.sleep(2)
            continue
        except Exception as e:
            logging.error(f'{url_name} -- Unexpected error: {e}')
            with open(error_file, 'a') as ef:
                ef.write(f'{url_name}\n')
            return None

    logging.error(f'{url_name} -- Failed to obtain valid response after {max_attempts} attempts')
    with open(error_file, 'a') as ef:
        ef.write(f'{url_name}\n')
    return None


def callback(ch, method, properties, body):
    month = current_month()
    scam_file = f'results/scams_{month}.txt'
    not_scam_file = f'results/not_scams_{month}.txt'
    error_file = f'results/errors_{month}.txt'

    try:
        log_data = json.loads(body)
    except json.JSONDecodeError as e:
        logging.error(f'Failed to decode message from {SOURCE_QUEUE_NAME}: {e}')
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    domain = log_data.get('url')
    text = log_data.get('text')
    if not domain or not text:
        logging.error(f'Message missing url/text, skipping: {log_data}')
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    if domain in done_domains:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    start_time = time.time()
    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    chat_response = run_ollama_request(domain, base_messages, error_file)

    if chat_response:
        clean_json = json.dumps(chat_response, indent=None)
        if chat_response['answer'] == 'yes':
            with open(scam_file, 'a') as sf:
                sf.write(f'{domain},{clean_json}\n')
            # Hand off to the crawler stage (paper §2.4: Account Creation
            # and Wallet Extraction). Carry along whatever IOC/IP context
            # recv.py already extracted so the crawler doesn't need to
            # re-derive it.
            confirmed_record = {
                "url": domain,
                "reason": chat_response.get('reason'),
                "ioc": log_data.get('ioc'),
                "ip_info": log_data.get('ip_info'),
                "title": log_data.get('title'),
            }
            confirmed_payload = json.dumps(confirmed_record)
            # scam_file above only records the LLM's answer/reason -- ioc,
            # ip_info, and title only ever existed inside this queue
            # message. Once the crawler acks it, that context is gone
            # unless it's written down here first.
            confirmed_scams_file = f'results/confirmed_scams_{month}.jsonl'
            with open(confirmed_scams_file, 'a') as csf:
                csf.write(confirmed_payload + '\n')
            ch.basic_publish(
                exchange='',
                routing_key=CONFIRMED_QUEUE_NAME,
                body=confirmed_payload,
                properties=pika.BasicProperties(delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE),
            )
        elif chat_response['answer'] == 'no':
            with open(not_scam_file, 'a') as nsf:
                nsf.write(f'{domain},{clean_json}\n')

        done_domains.add(domain)
        with open('done.txt', 'a') as donefile:
            donefile.write(f'{domain}\n')
        logging.info(f'Successfully processed and logged domain {domain}')
    else:
        logging.error(f'Failed to process domain {domain}')

    processing_time = time.time() - start_time
    logging.info(f'Processed domain {domain} in {processing_time:.2f} seconds.')
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main():
    os.makedirs('results', exist_ok=True)
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
            channel = connection.channel()
            channel.queue_declare(queue=SOURCE_QUEUE_NAME, durable=True)
            channel.queue_declare(queue=CONFIRMED_QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=SOURCE_QUEUE_NAME, on_message_callback=callback)
            logging.info(f"Consuming from '{SOURCE_QUEUE_NAME}', publishing confirmed scams to '{CONFIRMED_QUEUE_NAME}'.")
            channel.start_consuming()
        except pika.exceptions.StreamLostError as e:
            logging.error(f"RabbitMQ connection lost, reconnecting: {e}")
            time.sleep(10)
        except pika.exceptions.AMQPConnectionError as e:
            logging.error(f"RabbitMQ connection error, retrying: {e}")
            time.sleep(10)


if __name__ == '__main__':
    main()
