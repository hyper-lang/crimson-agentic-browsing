import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Was hardcoded to '/home/ubuntu/cryptoscams/datacollection/testing/chrome-unpacked/chrome-linux64/chrome'
# -- a path specific to the original authors' own VM, from a manually
# downloaded/unpacked Chrome build (likely to pin a specific version
# against a matching chromedriver). Defaults here point at the
# apt-installed chromium/chromium-driver baked into the crimson-python-env
# image instead, since apt keeps those two version-matched automatically.
CHROME_BINARY = os.environ.get('CRIMSON_CHROME_BINARY', '/usr/bin/chromium')
CHROMEDRIVER_PATH = os.environ.get('CRIMSON_CHROMEDRIVER_PATH', '/usr/bin/chromedriver')

class SeleniumScreenshot:
    def __init__(self):
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--incognito")
        chrome_options.add_argument('--remote-debugging-pipe')
        chrome_options.add_argument("--window-size=1024,768")
        chrome_options.binary_location = CHROME_BINARY
        self.options = chrome_options

    def take_screenshot(self, url, curr_date, path, SYSNO):
        for attempt in range(2):
            if self.screenshot_retrier(url, curr_date, path, SYSNO):
                return True
        time.sleep(0.5)
        return False

    def screenshot_retrier(self, url, curr_date,  path, SYSNO):
        service = Service(executable_path=CHROMEDRIVER_PATH)
        browser = None
        try:
            # No service.start() here: webdriver.Chrome() starts the service
            # itself, and Service.start() spawns a new chromedriver every call,
            # so starting it twice orphaned one chromedriver per attempt
            # (~1000 per worker after a day, until the OOM killer stepped in).
            browser = webdriver.Chrome(options=self.options, service=service)
            browser.set_page_load_timeout(20)
            browser.get("http://" + url)
            WebDriverWait(browser, 20).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            browser.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            browser.execute_script("window.scrollTo(0, 0);")
            S = lambda X: browser.execute_script('return document.body.parentNode.scroll' + X)
            browser.set_window_size(S('Width'), S('Height'))
            browser.find_element(By.TAG_NAME, 'body').screenshot(path + '/full_page.png')
            return True
        except Exception as e:
            return False
        finally:
            if browser:
                browser.quit()
            # Covers webdriver.Chrome() failing after it started chromedriver
            # (browser is None then, so quit() never ran). stop() can raise on
            # a never-started service; don't let that mask the real error.
            try:
                service.stop()
            except Exception:
                pass
