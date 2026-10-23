"""`wallet-browser` command-line entry point."""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys

from dotenv import find_dotenv, load_dotenv

DEFAULT_RESULTS_FILE = 'results/wallet_extraction.jsonl'


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() != 'false'


def read_targets(path: str) -> list[str]:
    with open(path) as f:
        lines = (line.split('#', 1)[0].strip() for line in f)
        return [line for line in lines if line]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    model = os.environ.get('WALLET_BROWSER_MODEL', 'sonnet')
    timeout = int(os.environ.get('WALLET_BROWSER_TIMEOUT', '600'))
    p = argparse.ArgumentParser(
        prog='wallet-browser',
        description="Sign up on crypto scam sites and extract the deposit/wallet addresses "
                    "they hand out (Scrapling stealth browser driven by Claude Code).")
    p.add_argument('targets', nargs='*', help="Domain(s) or URL(s) to crawl")
    p.add_argument('-f', '--file', action='append', default=[], metavar='TXT',
                   help="Text file of targets, one per line; blank lines and # comments "
                        "skipped (repeatable)")
    p.add_argument('-o', '--output', default=DEFAULT_RESULTS_FILE,
                   help=f"JSONL file to append results to (default: {DEFAULT_RESULTS_FILE}; "
                        "'-' to only print to stdout)")
    p.add_argument('--timeout', type=int, default=timeout,
                   help=f"Per-site crawl budget in seconds (default: {timeout})")
    p.add_argument('--model', default=model, help=f"Claude model alias or ID (default: {model})")
    p.add_argument('--headed', action='store_true',
                   default=not _env_bool('WALLET_BROWSER_HEADLESS', True),
                   help="Show the browser window (needs a display)")
    p.add_argument('--profile', metavar='DIR',
                   help="Reuse a browser profile (its saved cookies/session carry over "
                        "between runs)")
    p.add_argument('--no-solve-cloudflare', dest='solve_cloudflare', action='store_false',
                   default=_env_bool('WALLET_BROWSER_SOLVE_CLOUDFLARE', True),
                   help="Don't run Scrapling's Cloudflare solver; record walled sites as BLOCKED")
    p.add_argument('--no-solve-captcha', dest='solve_captcha', action='store_false',
                   default=_env_bool('WALLET_BROWSER_SOLVE_CAPTCHA', True),
                   help="Don't have Claude read image/math CAPTCHAs on signup/login forms")
    p.add_argument('-q', '--quiet', action='store_true', help="Only log warnings and errors")
    p.add_argument('--install-browser', action='store_true',
                   help="Download the patched Chromium the crawler uses, then exit")
    args = p.parse_intermixed_args(argv)
    if args.install_browser:
        return args
    for path in args.file:
        try:
            args.targets += read_targets(path)
        except OSError as e:
            p.error(f"can't read {path}: {e.strerror}")
    args.targets = list(dict.fromkeys(args.targets))
    if not args.targets:
        p.error("give at least one target, or a file of them with -f")
    return args


def install_browser() -> int:
    # In the tool's own environment, so it matches the installed patchright.
    return subprocess.call([sys.executable, '-m', 'patchright', 'install', 'chromium'])


def _browser_installed() -> bool:
    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        return os.path.exists(pw.chromium.executable_path)


def main(argv: list[str] | None = None) -> None:
    # A .env in the current directory supplies defaults; real env vars win.
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args(argv)
    if args.install_browser:
        sys.exit(install_browser())

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format='%(asctime)s:%(levelname)s:%(message)s',
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    # Scrapling has its own stderr handler; don't print its lines twice.
    logging.getLogger('scrapling').propagate = False

    from .claude_cli import claude_bin
    from .crawler import chrome_path, crawl
    if not shutil.which(claude_bin()):
        sys.exit("Claude Code CLI not found -- install it and run `claude` once to log in "
                 "(or point CLAUDE_BIN at it).")
    if not chrome_path() and not _browser_installed():
        sys.exit("Browser not installed -- run `wallet-browser --install-browser` first.")

    for i, target in enumerate(args.targets, 1):
        if len(args.targets) > 1:
            logging.info(f"[{i}/{len(args.targets)}] {target}")
        output = crawl(target, model=args.model, headless=not args.headed,
                       profile_dir=args.profile, solve_cf=args.solve_cloudflare,
                       solve_captcha=args.solve_captcha, timeout=args.timeout)
        print(json.dumps(output), flush=True)
        if args.output != '-':
            os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
            with open(args.output, 'a') as f:
                f.write(json.dumps(output) + '\n')


if __name__ == '__main__':
    main()
