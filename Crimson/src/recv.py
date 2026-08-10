import pika, sys, os
import pytz
from datetime import datetime, timezone, timedelta, date
import time
import utils.keyword_utils as kw_utils
import utils.screenshot
import requests
import html2text
import pytesseract
import whois
import re
from PIL import Image
import signal
import json
import subprocess
import socket
import logging
from logging.handlers import TimedRotatingFileHandler
from cachetools import LRUCache
from bs4 import BeautifulSoup
import iocsearcher
from iocsearcher.searcher import Searcher
from nltk.stem import PorterStemmer

stemmer = PorterStemmer()

# Table 11, Appendix D (paper 3696410.3714588). Hoisted to module scope --
# these were previously rebuilt on every single OCR() call.
INVEST_WORDS = [
    "crypto", "fx", "earn", "deposit", "trade", "capital", "invest", "global",
    "bit", "mining", "ltd", "finance", "trade", "miner", "trust", "profit",
    "asset", "cardano", "funding", "capitals", "fund", "limited", "chain",
    "digital", "btc", "assets", "wealth", "coin", "option", "prime", "bitcoin",
    "exchange", "money", "eth", "ethereum", "cryptocurrency",
]
COIN_WORDS = [
    "invest", "crypto", "bitcoin", "ethereum", "cardano", "ripple", "binance",
    "shiba inu", "dogecoin", "solana", "tether", "tron", "polkadot", "eth",
    "btc", "xrp", "ada", "bnb", "shib", "doge", "sol", "usdt", "trx", "dot",
    "algo", "litecoin", "chainlink", "uniswap", "pancakeswap", "avalanche",
    "neo", "iota", "aave", "luna", "synthetix", "theta", "grt", "1inch",
    "sushi", "matic", "btcusd", "usdbtc", "ethusd", "usdeth", "adausd",
    "usdada", "xrpusd", "usdxrp", "bnbusd", "usdbnb", "shibusd", "usdshib",
    "dogeusd", "usddoge", "solusd", "usdsol", "usdtusd", "usdusdt",
]
CONTEXT_WORDS = [
    "deposit", "withdraw", "reward", "growth", "gain", "capital", "potential",
    "wallet", "safe", "secure", "fund", "profit", "insurance", "wealth",
    "send", "transfer", "sell", "buy", "trade", "asset", "client", "solution",
    "funding",
]
# Paper §2.2: OCR-extracted tokens are stemmed before comparison, same as
# the domain-based filter in keyword_utils.py.
STEMMED_INVEST_WORDS = {stemmer.stem(w) for w in INVEST_WORDS}
STEMMED_COIN_WORDS = {stemmer.stem(w) for w in COIN_WORDS}
STEMMED_CONTEXT_WORDS = {stemmer.stem(w) for w in CONTEXT_WORDS}

# --- Configurable host (was hardcoded to a specific IP) ---
# QUEUE_IP is used both as the RabbitMQ host and as the rsync target host,
# matching the original script's assumption that the queue box and the
# central storage box are the same machine. Override with CRIMSON_HOST.
QUEUE_IP = os.environ.get('CRIMSON_HOST', 'localhost')

searcher = Searcher() # initialize IOC searcher obj https://github.com/malicialab/iocsearcher
os.environ['OMP_THREAD_LIMIT'] = '1'
SYSNO = sys.argv[1] # number to identify the worker number for logging
SCREENNO = sys.argv[2]
newYorkTz = pytz.timezone("America/New_York")
text_converter = html2text.HTML2Text()
text_converter.ignore_links = True  # Ignore hyperlinks
text_converter.ignore_images = True  # Ignore images
unavailable_domains = 0
domains_checked = 0
less_words_domains = 0
ocr_domains = 0
selenium_obj = utils.screenshot.SeleniumScreenshot()
REMOTE_IP = QUEUE_IP
REMOTE_USER = os.environ.get('CRIMSON_REMOTE_USER', 'ubuntu')
PRIVATE_KEY_PATH = os.environ.get('CRIMSON_SSH_KEY_PATH', '../../.ssh/id_ed25519')
# Was a literal 'add_central_server_path' placeholder for both sync() calls,
# which meant results/ and check/ (unrelated directories) would rsync into
# the exact same remote path. Split into two so they land separately.
REMOTE_RESULTS_PATH = os.environ.get('CRIMSON_REMOTE_RESULTS_PATH', 'crimson-central/results/')
REMOTE_CHECK_PATH = os.environ.get('CRIMSON_REMOTE_CHECK_PATH', 'crimson-central/check/')
OCR_RESULTS_QUEUE = 'ocr_results'  # published to on a positive match; consumed by validate.py
visited_cache = LRUCache(maxsize=1000)

def ensure_directory_exists(directory_path):
    if not os.path.exists(directory_path):
        try:
            os.makedirs(directory_path)
        except OSError as e:
            log(f"Error creating directory {directory_path}: {e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'errors.txt', 'a')

def sync ( url_name, curr_date, local_file_path, remote_file_path ):
    try:
        subprocess.run([ 'rsync', '-avz', '-e', f'ssh -i {PRIVATE_KEY_PATH}', local_file_path, f'{REMOTE_USER}@{REMOTE_IP}:{remote_file_path}' ], check=True, stdout=subprocess.DEVNULL)
    except Exception as e:
        log(f"{url_name},{e}", curr_date, 'rsync-failure.txt', 'a')

def OCR(url_name, curr_date, html_content):
    global ocr_domains
    ocr_domains += 1
    log(ocr_domains, curr_date, 'ocr_domains.txt', 'w')
    def find_intersection(list1, list2): return list(set(list1).intersection(set(list2)))
    def clean_strings(string_list): return [stemmer.stem(re.sub(r'[^A-Za-z0-9]', '', s)) for s in string_list]
    sspath = f'data/{SYSNO}/screenshots/{curr_date}/{url_name}/'
    if os.path.exists(sspath) and 'full_page.png' not in os.listdir(sspath): return False
    try:
        text = str(pytesseract.image_to_string(Image.open(f'{sspath}/full_page.png'))).lower().encode('utf-8')
        text = re.sub(r'\s+', ' ', re.sub(r'[^\w\s,.!?\-\'"]+', '', re.sub(r'[\n\r\t]+', ' ', re.sub(r'[^\x00-\x7F]+', ' ', text.decode('utf-8', 'replace'))))).strip()
        text_splits = clean_strings(text.split()) # without punctuations, stemmed
        soup = BeautifulSoup(html_content, 'lxml')
        html_splits = clean_strings(soup.get_text(separator=' ', strip=True).split())
        text_splits = html_splits + text_splits
    except Exception as e:
        log(f"{url_name},OCR (),{e}", curr_date, 'errors.txt', 'a')
        return False
    matches = [find_intersection(text_splits, STEMMED_INVEST_WORDS), find_intersection(text_splits, STEMMED_COIN_WORDS), find_intersection(text_splits, STEMMED_CONTEXT_WORDS)]
    if len(matches[0]) and len(matches[2]) and len(matches[1]):
        return text
    return False


def getIPInfo (url_name):
    curr_date = str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:]
    def IPAPI ( ip_address ):
        try:
            response = requests.get(f'http://ip-api.com/json/{ip_address}?fields=status,message,countryCode,region,city,lat,lon,isp,org,query')
            data = response.json()
            return data
        except Exception as e:
            log(f"{url_name},IPAPI (),{e}", curr_date, 'errors.txt', 'a')
            return None
    try:
        return IPAPI(socket.gethostbyname(url_name))
    except Exception as e:
        log(f"{url_name},getIPInfo (),{e}", curr_date, 'errors.txt', 'a')
        return None

def getioc (url_name, curr_date, html_content):
    try:
        ioc = searcher.search_data(html_content, targets={'bitcoin', 'bitcoincash', 'cardano', 'dashcoin', 'dogecoin', 'ethereum', 'litecoin', 'monero', 'ripple', 'tezos', 'tronix', 'zcash', 'webmoney', 'onionAddress', 'email', 'phoneNumber', 'facebookHandle', 'githubHandle', 'instagramHandle', 'linkedinHandle', 'pinterestHandle', 'telegramHandle', 'twitterHandle', 'whatsappHandle', 'youtubeHandle', 'youtubeChannel'})
        ioc_dict = {}
        for item in ioc:
            item = str(item).split('\t')
            ioc_dict.update({item[0]: item[1]})
        return ioc_dict
    except Exception as e:
        log(f"{url_name},getioc (),{e}", curr_date, 'errors.txt', 'a')
        return None
def get_website_title(html_content):
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        title_tag = soup.find('title')
        if title_tag is not None:
            title = title_tag.get_text()
            return title
        else: return "No Title."
    except requests.exceptions.RequestException as e:
        return "No Title."
def is_domain_available (url_name, curr_date):
    global less_words_domains
    def extract_js_libraries(html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        script_tags = soup.find_all('script', src=True)
        js_libraries = [tag['src'] for tag in script_tags]
        return js_libraries
    try:
        response = requests.get('http://' + url_name, timeout=10)
        if str(response.status_code)[0] not in ['4', '5']: #200s,300s
            html_content = response.text
            plain_text = str(text_converter.handle(response.text)).split()
            if len(plain_text) < 20:
                less_words_domains += 1
                log(less_words_domains, curr_date, 'less_words_domains.txt', 'w')
                return False
            js_libraries = extract_js_libraries(html_content)
            return (js_libraries, html_content)
        else:
            log(f"{url_name}", curr_date, 'unavailable.txt', 'a')
            return False
    except Exception as e:
        return False

def get_domain_creation_date(url_name, curr_date):
    try:
        domain_info = whois.whois(url_name)
        if domain_info.creation_date:
            creation_dates = domain_info.creation_date
            if isinstance(creation_dates, list):
                return creation_dates[0]  # Use the first date if it's a list
            else:
                return creation_dates  # Return the single datetime value
        else:
            return "Creation date information not found."
    except Exception as e:
        log(f"{url_name},get_domain_creation_date (),{e}", curr_date, 'errors.txt', 'a')
        return "Creation date information not found."

def handlePositives (url_name, text, js_libraries, html_content, path, curr_date, channel):
    check_path = f"data/{SYSNO}/check/{curr_date}/"
    ioc = getioc(url_name, curr_date, html_content)
    ensure_directory_exists (check_path)
    os.system(f"cp {path} {check_path} -r")
    ip_info = getIPInfo (url_name)

    domain_creation_date = get_domain_creation_date (url_name , curr_date) 
    if domain_creation_date == "Creation date information not found." or not isinstance(domain_creation_date, datetime):
        domain_creation_date = 'NotFound'
    else:
        domain_creation_date = str(int(domain_creation_date.timestamp()))
    log_data = {
        "url": url_name,
        "title": get_website_title(html_content),
        "creation_date": domain_creation_date,
        "ip_info": ip_info,
        "ioc": ioc,
        "js_libraries": js_libraries,
        "text": text
    }
    log_result(json.dumps(log_data), curr_date, 'results.log', 'a')
    # Bridge to validate.py: publish the same record it used to have to read
    # back out of a hand-maintained data/ocr_results_{month}.csv.
    try:
        channel.basic_publish(
            exchange='',
            routing_key=OCR_RESULTS_QUEUE,
            body=json.dumps(log_data),
            properties=pika.BasicProperties(delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE)
        )
    except Exception as e:
        log(f"{url_name},handlePositives publish (),{e}", curr_date, 'errors.txt', 'a')

def log_result (url_name, curr_date, filename, mode='a'):
    filename = f"{filename}.{curr_date}"
    ensure_directory_exists (f"results/{SYSNO}/")
    with open(os.path.join('results', SYSNO, filename), mode) as f:
        f.write(f'{datetime.now(timezone(timedelta(hours=-5))).strftime("%H:%M:%S")}:\t{url_name}\n')

def check(url_name, curr_date, channel):
    global unavailable_domains
    global domains_checked
    domains_checked += 1
    log(domains_checked, curr_date, 'domains_checked.txt', 'w')
    '''
    Check Availability of domain
    '''
    start = time.time()
    domain_availability = is_domain_available (url_name, curr_date)
    if not domain_availability:
        unavailable_domains += 1
        log(unavailable_domains, curr_date, 'unavailable_domains.txt', 'w')
        end = time.time()
        log(f"{url_name},{end-start}", curr_date, 'time_availability.txt', 'a')
        return f"unreachable."
    js_libraries, html_content = domain_availability
    end = time.time()
    log(f"{url_name},{end-start}", curr_date, 'time_availability.txt', 'a')

    '''
    Full-page Screenshot
    '''
    start = time.time()
    ensure_directory_exists (f"data/{SYSNO}/screenshots/{curr_date}")
    sspath = f"data/{SYSNO}/screenshots/{curr_date}/{url_name}/"
    ensure_directory_exists (sspath)
    # Raw HTML was previously never persisted -- it only existed in memory
    # during OCR/IOC extraction. Written here so it shares the same
    # lifecycle as the screenshot: deleted on a screenshot/OCR failure
    # (below), kept and copied into check/ on an actual positive match
    # (handlePositives' `cp {path} {check_path} -r` already picks it up
    # automatically since it copies the whole sspath directory).
    try:
        with open(os.path.join(sspath, 'page.html'), 'w', encoding='utf-8') as html_file:
            html_file.write(html_content)
    except Exception as e:
        log(f"{url_name},write page.html (),{e}", curr_date, 'errors.txt', 'a')
    try:
        ss = selenium_obj.take_screenshot(url_name, curr_date, sspath, SYSNO)
        if not ss:
            if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
            end = time.time()
            log(f"{url_name},{end-start}", curr_date, 'time_screenshot.txt', 'a')
            return f"screenshot failure."
        end = time.time()
        log(f"{url_name},{end-start}", curr_date, 'time_screenshot.txt', 'a')
    except Exception as e:
        log(f"{url_name},selenium_obj.take_screenshot (),{e}", curr_date, 'errors.txt', 'a')
        end = time.time()
        log(f"{url_name},{end-start}", curr_date, 'time_screenshot.txt', 'a')
        return "screenshot failure"

    '''
    Perform OCR
    '''
    start = time.time()
    text = OCR(url_name, curr_date, html_content)
    if not text: # OCR Failure, so deleting the files.
        if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
        end = time.time()
        log(f"{url_name},{end-start}", curr_date, 'time_OCR.txt', 'a')
        return f"OCR failure."
    else: # positive! saving in check folder.
        handlePositives (url_name, text, js_libraries, html_content, sspath, curr_date, channel)
    end = time.time()
    log(f"{url_name},{end-start}", curr_date, 'time_OCR.txt', 'a')
    return f"Scam Found!"



def log(url_name, curr_date, filename, mode='a'):
    ensure_directory_exists (f"logs/{SYSNO}/{curr_date}")
    with open(os.path.join('logs', SYSNO, curr_date, filename), mode) as f:
        f.write(f'{datetime.now(timezone(timedelta(hours=-5))).strftime("%H:%M:%S")}\t{SCREENNO}:\t{url_name}\n')

def mkdirs ():
    if SYSNO not in os.listdir('data/'):
        ensure_directory_exists (f"data/{SYSNO}")
        ensure_directory_exists (f"data/{SYSNO}/screenshots")
        ensure_directory_exists (f"data/{SYSNO}/ocr")
        ensure_directory_exists (f"data/{SYSNO}/check")
    if SYSNO not in os.listdir('logs/'):
        ensure_directory_exists (f"logs/{SYSNO}")

def main():
    def timeout_handler(signum, frame):
        raise TimeoutError("Function execution timed out")
    def callback(ch, method, properties, body):
        global visited_cache
        url_name = body.decode('utf-8')
        curr_date = str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:]
        log(f" [x] Received {url_name}", curr_date, 'worker.txt', 'a')
        if url_name in visited_cache:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            log(f"[x] Dup {url_name}", curr_date, 'worker.txt', 'a')
        else:
            visited_cache[url_name] = True
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(55)
            try:
                result = check(url_name, curr_date, ch)
                sys.stdout.flush()
            except TimeoutError as e:
                log(f"{url_name},[x] Timeout occurred,{e}", curr_date, 'errors.txt', 'a')
                result = "Timeout"
            signal.alarm(0)
            # print(f"[x] Done {url_name} {result}")
            log(f"[x] Done {url_name} {result}", curr_date, 'worker.txt', 'a')
            sync(url_name, curr_date, f"results/{SYSNO}/", REMOTE_RESULTS_PATH)
            if result == "Scam Found!":
                # data/{SYSNO}/check/ is only ever populated on a match --
                # syncing it unconditionally on every domain just produced a
                # failed rsync (logged to rsync-failure.txt) for every miss.
                sync(url_name, curr_date, f"data/{SYSNO}/check/", REMOTE_CHECK_PATH)
            ch.basic_ack(delivery_tag=method.delivery_tag)
    
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=QUEUE_IP))
            channel = connection.channel()
            channel.queue_declare(queue='cryptoscams', durable=True)
            channel.queue_declare(queue=OCR_RESULTS_QUEUE, durable=True)
            print(' [*] Waiting for messages. To exit press CTRL+C')
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue='cryptoscams', on_message_callback=callback)
            channel.start_consuming()
        except pika.exceptions.StreamLostError as e:
            log(f"RabbitMQ Connection Lost. Reconnecting...,{e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'errors.txt', 'a')
            time.sleep(10)

if __name__ == '__main__':
    try:
        mkdirs ()
        main()
    except Exception as e:
        log (f"exit {e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'exit.txt')
        try:
            log (f"sys.exit(0) {e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'exit.txt')
            sys.exit(0)
        except SystemExit as e_:
            log (f"SystemExit {e_}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'exit.txt')
            os._exit(0)
