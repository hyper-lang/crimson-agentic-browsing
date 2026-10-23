"""Sign up on a site and extract the crypto deposit addresses it reveals.

The browser is Scrapling's AsyncStealthySession (patchright Chromium with
fingerprint hardening and a Cloudflare Turnstile/interstitial solver). Each
step is driven by Claude through the local Claude Code CLI (see page_agent.py
and claude_cli.py), so no API key is needed.

The signup -> deposit -> extract flow is scripted one act()/extract() at a
time, inside Scrapling's page_action hook.
"""
import asyncio
import ipaddress
import logging
import os
import random
import time
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel
from scrapling.fetchers import AsyncStealthySession

from .claude_cli import ClaudeRefusal
from .page_agent import PageAgent

# Extra signup/login submits after an image CAPTCHA is rejected.
CAPTCHA_RETRIES = 2
IN_DOCKER = os.path.exists('/.dockerenv')

# Common deposit currencies, used when the deposit form's own crypto options
# can't be read.
COINS = ['BTC', 'ETH', 'USDT (ERC-20)', 'USDT (TRC-20)', 'BNB', 'USDC', 'TRX']
MAX_PAYMENT_OPTIONS = 12


class PostSignup(BaseModel):
    reached_dashboard: bool
    # A fixed category to branch on; Claude's free-text explanation goes in detail.
    blocker: Literal['none', 'no_signup_form', 'login_required', 'captcha',
                     'email_verification', 'sms_or_kyc', 'other']
    detail: str


class PaymentOptions(BaseModel):
    options: list[str]  # crypto payment methods as labelled on the form


class WalletAddresses(BaseModel):
    addresses: list[str]


class SignupResult(BaseModel):
    signed_up: bool
    final_url: str
    wallet_addresses_found: list[str]
    notes: str


def chrome_path() -> str | None:
    # Default is patchright's bundled Chromium: headless Google Chrome gets 403'd
    # by Cloudflare's interstitial where the bundled build passes. Set CHROME_PATH
    # only to force a specific binary.
    return os.environ.get('CHROME_PATH') or None


def _in_scope(host: str, target_host: str) -> bool:
    host = host.lower().removeprefix('www.')
    return host == target_host or host.endswith('.' + target_host)


def _is_internal(host: str) -> bool:
    if host in ('localhost', 'host.docker.internal') or host.endswith('.localhost'):
        return True
    try:
        ip = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _scope_guard(target_host: str):
    """Route handler: keep top-level navigation on the target domain + subdomains,
    and block any request to internal addresses (scope/SSRF guard). Sub-resources
    and iframes (CDNs, the Cloudflare challenge frame) are allowed through."""
    async def handler(route):
        req = route.request
        host = (urlparse(req.url).hostname or '').lower()
        try:
            top_nav = req.is_navigation_request() and req.frame.parent_frame is None
        except Exception:
            top_nav = False
        if host and (_is_internal(host) or (top_nav and not _in_scope(host, target_host))):
            logging.info(f"  scope guard blocked {req.url[:120]}")
            await route.abort('blockedbyclient')
        else:
            await route.fallback()
    return handler


async def _bot_wall(page) -> str | None:
    """Heuristic: are we stuck on a bot wall? Returns None, 'phishing' (Cloudflare
    has already flagged the site and shows its phishing warning), 'firewall' (a
    hard Cloudflare/WAF block that no solver can clear) or 'challenge' (an
    interstitial or CAPTCHA page that might be solvable). A page merely
    mentioning Cloudflare, e.g. a "protected by Cloudflare" footer, is not a wall."""
    try:
        title = (await page.title() or '').lower()
        html = await page.content()
        text = (await page.evaluate("() => document.body ? document.body.innerText : ''")).lower()
    except Exception:
        return None
    if 'suspected phishing' in title:
        return 'phishing'
    if 'attention required' in title or 'sorry, you have been blocked' in text:
        return 'firewall'
    if 'just a moment' in title or "cType: '" in html:  # Cloudflare interstitial
        return 'challenge'
    markers = ('verify you are human', 'checking your browser', 'checking if the site connection',
               'needs to review the security', 'complete the captcha')
    if any(m in text for m in markers) and 'deposit' not in text:
        return 'challenge'
    return None


async def _run_flow(page, session, target_host: str, persona: dict, model: str,
                    solve_cf: bool, solve_captcha: bool) -> SignupResult:
    """The flow, plus refusal handling: if Claude declines a step, stop the site and
    record where, keeping whatever was already found. No retry or rewording."""
    state = {'signed_up': False, 'found': []}
    try:
        return await _flow(page, session, target_host, persona, model, solve_cf,
                           solve_captcha, state)
    except ClaudeRefusal as e:
        step = ' '.join(e.step.split())[:80]
        logging.warning(f"  Claude refused step '{step}': {e.detail[:160]}")
        kept = f"; kept {len(state['found'])} address(es) found before it" if state['found'] else ""
        return SignupResult(signed_up=state['signed_up'], final_url=page.url,
                            wallet_addresses_found=state['found'],
                            notes=f"REFUSED: Claude safety classifier at step '{step}'{kept}.")


async def _flow(page, session, target_host: str, persona: dict, model: str,
                solve_cf: bool, solve_captcha: bool, state: dict) -> SignupResult:
    agent = PageAgent(page, model, solve_captcha)

    async def walled(stage: str) -> SignupResult | None:
        # Playwright routes only the first hop of a redirect chain, so an HTTP
        # redirect can carry us off the target domain past _scope_guard.
        host = (urlparse(page.url).hostname or '').lower()
        if page.url.startswith('http') and not _in_scope(host, target_host):
            return SignupResult(signed_up=False, final_url=page.url, wallet_addresses_found=[],
                                notes=f"Left target domain {stage}: redirected to {host}; stopped.")
        kind = await _bot_wall(page)
        if kind is None:
            return None
        if kind == 'phishing':
            # Deliberately not clicking "Ignore & Proceed": record the takedown instead.
            return SignupResult(signed_up=False, final_url=page.url, wallet_addresses_found=[],
                                notes=f"FLAGGED: Cloudflare 'Suspected Phishing' warning {stage}.")
        if kind == 'firewall':
            return SignupResult(signed_up=False, final_url=page.url, wallet_addresses_found=[],
                                notes=f"BLOCKED: Cloudflare/WAF firewall block {stage} (not a solvable challenge).")
        if solve_cf:
            # Run Scrapling's solver only once a wall is actually showing: letting
            # fetch() run it on every landing page logs a spurious error on clean
            # sites, and a challenge can reappear after navigating (e.g. /register).
            logging.info(f"  bot wall {stage}; running Scrapling's Cloudflare solver")
            try:
                await session._cloudflare_solver(page)
            except Exception as e:
                logging.info(f"  solver error: {e}")
            if await _bot_wall(page) is None:
                return None
        how = "persisted after the Cloudflare solver" if solve_cf else "solver disabled"
        return SignupResult(signed_up=False, final_url=page.url, wallet_addresses_found=[],
                            notes=f"BLOCKED: bot-protection wall (Cloudflare/CAPTCHA) {stage}; {how}.")

    async def solve_turnstile():
        # An embedded Turnstile widget on the form: Scrapling's solver clicks it.
        if solve_cf and 'challenges.cloudflare.com/turnstile' in await page.content():
            logging.info("  embedded Turnstile on form; running Scrapling's solver")
            try:
                await session._cloudflare_solver(page)
            except Exception as e:
                logging.info(f"  solver error: {e}")

    if blocked := await walled("on landing"):
        return blocked

    # --- reach + complete signup ---
    await agent.act("If this page is not already a registration form, find and click a "
                    "'Sign Up', 'Register', 'Join', 'Open Account', or 'Get Started' "
                    "link or button.")
    if blocked := await walled("reaching signup"):
        return blocked

    await agent.act(
        f"Fill the registration form: full name '{persona['name']}', email "
        f"'{persona['email']}', username '{persona['username']}' if there is a username "
        f"field, password '{persona['password']}' (and the same in any confirm-password "
        f"field), phone '{persona['phone']}' if required, country 'United States' if "
        f"required. Use believable placeholders for any other required field and skip "
        f"optional ones. If the form has a simple math question or an image CAPTCHA / "
        f"verification code, fill its answer too. Do not submit yet.")
    await solve_turnstile()
    await agent.act("Check every required checkbox (terms of service, age, etc.), then "
                    "submit the registration form.")

    # --- assess where we landed ---
    async def assess() -> PostSignup:
        # Let post-submit redirects ("Account created!" -> /dashboard) finish first.
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        st = await agent.extract(
            "Did we reach a logged-in account dashboard or home area? If not, pick the main "
            "blocker: 'no_signup_form' (the site has no account registration at all, e.g. a "
            "brochure site or only a newsletter box), 'login_required' (signup went through "
            "and we now face a login form), 'captcha' (including a wrong/missing CAPTCHA "
            "code or security check), 'email_verification', 'sms_or_kyc', or 'other'. Use "
            "'none' if we reached the dashboard. Explain briefly in 'detail'.", PostSignup)
        logging.info(f"  after signup: reached_dashboard={st.reached_dashboard} "
                     f"blocker={st.blocker} ({st.detail[:120]})")
        return st

    attempts = 1

    async def retry_captcha(st: PostSignup, form: str) -> PostSignup:
        # Image CAPTCHAs are often misread, and a rejected submit shows a fresh image.
        nonlocal attempts
        tries = 0
        while (solve_captcha and tries < CAPTCHA_RETRIES and not st.reached_dashboard
               and st.blocker == 'captcha' and await _bot_wall(page) is None):
            tries += 1
            attempts += 1
            logging.info(f"  CAPTCHA retry {tries}/{CAPTCHA_RETRIES} on {form} form")
            await solve_turnstile()
            await agent.act(
                f"The {form} form was rejected or is still waiting on its CAPTCHA / security "
                f"check. Re-enter any fields that were cleared (password "
                f"'{persona['password']}' and its confirmation, email '{persona['email']}', "
                f"username '{persona['username']}'), answer the CAPTCHA again from the "
                f"attached image (it has probably changed) or the math question, tick "
                f"required checkboxes, and submit the form.")
            st = await assess()
        return st

    status = await retry_captcha(await assess(), 'signup')

    if not status.reached_dashboard and status.blocker != 'none':
        b = status.blocker
        if b == 'no_signup_form':
            return SignupResult(signed_up=False, final_url=page.url, wallet_addresses_found=[],
                                notes=f"No account signup found: {status.detail}")
        if b == 'other':
            # Often a "registration complete -> Continue to dashboard" page.
            await agent.act_steps(
                "If signup appears to have succeeded, follow the link or button that "
                "continues into the account (e.g. 'Continue to Dashboard', 'My Account', "
                "'Cabinet', 'Go to account'). Otherwise do nothing.")
            status = await assess()
            b = 'none' if status.reached_dashboard else status.blocker
        if b == 'login_required':
            await agent.act_steps(
                f"Log in: open the login form if needed, enter username "
                f"'{persona['username']}' (or email '{persona['email']}' if it asks for an "
                f"email) and password '{persona['password']}', answer any image CAPTCHA / "
                f"math question, and submit.")
            await solve_turnstile()
            status = await retry_captcha(await assess(), 'login')
            b = 'none' if status.reached_dashboard else status.blocker
        if b == 'captcha':
            page_wall = await _bot_wall(page) is not None
            if blocked := await walled("after signup"):
                return blocked
            if not page_wall:
                # An inline CAPTCHA widget on the form (reCAPTCHA, hCaptcha, ...),
                # not a full-page wall the solver handles.
                tried = f"{attempts} attempt(s)" if solve_captcha else "solving disabled"
                return SignupResult(signed_up=False, final_url=page.url,
                                    wallet_addresses_found=[],
                                    notes=f"BLOCKED: inline CAPTCHA on signup, {tried} "
                                          f"({status.detail})")
        elif b != 'none':
            return SignupResult(signed_up=False, final_url=page.url,
                                wallet_addresses_found=[],
                                notes=f"Signup incomplete ({b}): {status.detail}")

    if blocked := await walled("after signup/login"):
        return blocked
    state['signed_up'] = bool(status.reached_dashboard)

    # --- find deposit / wallet addresses ---
    await agent.act("Open the deposit / wallet / 'add funds' / 'receive crypto' section of "
                    "the account (look for nav items like Deposit, Wallet, Assets, Recharge, "
                    "Top Up, My Wallet).")
    # Many HYIP scripts land on a list of existing deposits first; the payment
    # options (and addresses) only appear on the "make a new deposit" form.
    await agent.act("If this page only lists existing deposits/investments or balances and "
                    "doesn't show payment methods, open the form to make a NEW deposit or "
                    "investment (e.g. 'Make Deposit', 'New Deposit', 'Invest', 'Add Funds', "
                    "'Top Up', 'Open plan', a link ending in ?add). Otherwise do nothing.")

    # HYIP scripts usually show the address only after choosing a coin, entering an
    # amount and continuing, so walk that per payment option and come back each time.
    deposit_url = page.url
    try:
        offered = (await agent.extract(
            "List every cryptocurrency payment option offered on this deposit form, exactly "
            "as labelled (include the network when shown, e.g. 'USDT TRC20'). Exclude fiat, "
            "card and 'account balance' options. Empty list if none.", PaymentOptions)).options
    except ClaudeRefusal:
        raise
    except Exception as e:
        logging.info(f"  couldn't read payment options: {e}")
        offered = []
    coins = list(dict.fromkeys(o.strip() for o in offered if o.strip()))[:MAX_PAYMENT_OPTIONS] or COINS
    logging.info(f"  payment options: {coins}")

    found: list[str] = state['found']
    for coin in coins:
        try:
            if page.url != deposit_url:
                await page.goto(deposit_url, wait_until='domcontentloaded')
                await page.wait_for_timeout(1500)
            if (await _bot_wall(page)) or not _in_scope(urlparse(page.url).hostname or '', target_host):
                break
            await agent.act_steps(
                f"On this deposit form, choose '{coin}' as the payment method/currency (if it "
                f"isn't offered, do nothing). If a plan must be chosen, pick the cheapest. If "
                f"an amount is required, enter the minimum allowed (or 50 if no minimum is "
                f"shown). Then click the button that continues to the payment details "
                f"(Continue, Deposit, Make Deposit, Next, Proceed, Generate address) so the "
                f"deposit address is shown. Never click anything confirming a payment was sent.")
            res = await agent.extract(
                f"Extract every crypto deposit/wallet address shown for {coin}. "
                "Addresses are long alphanumeric strings (BTC 1/3/bc1..., ETH 0x..., "
                "TRON T..., etc.). Include addresses in text, input fields, copy buttons "
                "and QR labels. Return an empty list if none are visible.", WalletAddresses)
            addresses = await agent.verify_addresses(res.addresses)
            logging.info(f"  {coin}: {len(addresses)} address(es)")
            for a in addresses:
                a = a.strip()
                if a and a not in found:
                    found.append(a)
        except ClaudeRefusal:
            raise
        except Exception as e:
            logging.info(f"  {coin}: {e}")

    note = (f"Signed up and reached deposit area; found {len(found)} address(es)."
            if found else
            "Reached the site but found no deposit addresses (may require KYC, only fiat, "
            "or a deposit flow the script didn't uncover).")
    return SignupResult(
        signed_up=bool(status.reached_dashboard),
        final_url=page.url,
        wallet_addresses_found=found,
        notes=note,
    )


async def signup_and_find_wallet(url: str, persona: dict, model: str, headless: bool,
                                 profile_dir: str | None, solve_cf: bool,
                                 solve_captcha: bool) -> SignupResult:
    target_host = (urlparse(url).hostname or url).lower().removeprefix('www.')
    chrome = chrome_path()
    outcome: dict = {}

    async def page_setup(page):
        await page.context.route('**/*', _scope_guard(target_host))

    async def page_action(page):
        # Scrapling logs and swallows page_action exceptions, so carry them out ourselves.
        try:
            outcome['result'] = await _run_flow(page, session, target_host, persona, model,
                                               solve_cf, solve_captcha)
        except BaseException as e:
            outcome['error'] = e
            if isinstance(e, asyncio.CancelledError):
                raise

    session = AsyncStealthySession(
        headless=headless,
        executable_path=chrome,
        user_data_dir=profile_dir or '',    # '' = fresh temp profile
        timeout=60000,
        retries=1,
        # Sandbox on except inside a container (Chrome's sandbox can't run as root there).
        additional_args={'chromium_sandbox': not IN_DOCKER},
    )
    async with session:
        await session.fetch(url, page_setup=page_setup, page_action=page_action)
    if 'error' in outcome:
        raise outcome['error']
    if 'result' not in outcome:
        raise RuntimeError("page never loaded (navigation failed before the agent ran)")
    return outcome['result']


def _persona() -> dict:
    n = random.randint(100000, 999999)
    return {
        'name': 'Jordan Lee',
        'email': f'jordan.lee.{n}@example.com',
        'username': f'jordanlee{n}',
        'password': f'Secure@Pass{n}!',
        'phone': f'+15559{random.randint(100,999)}{random.randint(1000,9999)}',
    }


def normalize_url(target: str) -> str:
    if '://' not in target:
        return 'https://' + target
    return target


def _run_site(url: str, opts: dict, timeout: int) -> SignupResult:
    return asyncio.run(asyncio.wait_for(signup_and_find_wallet(url, **opts), timeout=timeout))


def crawl(target: str, *, model: str, headless: bool = True, profile_dir: str | None = None,
          solve_cf: bool = True, solve_captcha: bool = True, timeout: int = 600) -> dict:
    """Crawl one target (bare domain or URL) and return its result record."""
    url = normalize_url(target)
    # Made here, not in the flow, so the record keeps the credentials even when the
    # crawl times out or crashes after the account was created.
    persona = _persona()
    account = {k: persona[k] for k in ('username', 'email', 'password', 'name', 'phone')}
    opts = dict(persona=persona, model=model, headless=headless, profile_dir=profile_dir,
                solve_cf=solve_cf, solve_captcha=solve_captcha)
    logging.info(f"Crawling {url}")
    start = time.time()
    try:
        try:
            result = _run_site(url, opts, timeout)
        except Exception as e:
            # Scam sites often serve HTTP fine but have broken/missing TLS. Only
            # retry when we picked https ourselves (bare-domain target).
            if '://' in target or 'ERR_SSL' not in str(e):
                raise
            logging.info(f"  TLS failed ({str(e).splitlines()[0][:80]}); retrying over http://")
            result = _run_site('http://' + target, opts, timeout)
        output = {"url": target, **result.model_dump(exclude={'notes'}),
                  "account": account, "notes": result.notes}
        logging.info(f"Finished {target} in {time.time()-start:.2f}s — "
                     f"wallets: {len(result.wallet_addresses_found)}")
    except asyncio.TimeoutError:
        output = {"url": target, "signed_up": False, "final_url": None,
                  "wallet_addresses_found": [], "account": account,
                  "notes": f"Crawl timed out after {timeout}s."}
        logging.error(f"Timed out on {target}")
    except Exception as e:
        logging.error(f"Crawl failed for {target}: {e}")
        output = {"url": target, "signed_up": False, "final_url": None,
                  "wallet_addresses_found": [], "account": account,
                  "notes": f"Crawl exception: {e}"}
    return output
