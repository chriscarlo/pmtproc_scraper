#!/usr/bin/env python3
"""
pmtproc.py – Passive GiveSendGo monitor
Launches the requested campaign page headed, lets the user interact,
records all network traffic to a HAR whose name contains the slug, then
terminates automatically the moment the browser window is closed.
"""
from pathlib import Path
import re
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright, Error as PWError
import threading, time
import subprocess, os, signal
from urllib.parse import urlparse
from collections import Counter
import json

# Optional dependency for accurate eTLD+1 resolution -----------------------
try:
    from tldextract import extract as _tx  # type: ignore
except ImportError:  # fall back to naive split if not available
    _tx = None

# ---------------------------------------------------------------------------
HAR_DIR = Path("/home/chris/openpilot/tools/bin")
HAR_DIR.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/125.0.0.0 Safari/537.36")

# Comprehensive payment-processor regex (user-supplied)
PAYMENT_RE = re.compile(
    r"stripe\.com|js\.stripe\.com|api\.stripe\.com|m\.stripe\.network|paypal\.com|paypalobjects\.com|"
    r"braintreepayments?\.com|adyen\.com|checkout\.adyen\.com|squareup\.com|cash\.app|authorize\.net|"
    r"cybersource|worldpay|worldpaygateway|globalpay|globalpayments|firstdata|fiserv|payeezy|klarna|"
    r"afterpay|affirm|2checkout|verifone|checkout\.com|amazonpay|payments\.amazon|amazon\.com/ap|"
    r"payoneer|bluepay|bluesnap|payline|chasepaymentech|ingenico|trustly|rapyd|payu|razorpay|mollie|"
    r"bolt\.com|pay\.google|googleapis\.com/payments|apple-pay|apple\.com/apple-pay|shopify-payments|"
    r"shopify|payment|checkout|card",
    re.I)

URL_RE = re.compile(r"https?://[^\s'\";,]+", re.I)

# ---------------------------------------------------------------------------

def kill_stale_chromium() -> None:
    """Force-kill leftover Playwright-spawned Chromium processes.
    When running under WSL it is quite common for the Chromium process that
    Playwright starts (identified by the ``--remote-debugging-pipe`` flag)
    to remain alive after the GUI window has been closed.  Before launching a
    fresh browser we therefore make a best-effort attempt to kill those
    stragglers so they do not interfere with subsequent runs.
    """
    patterns = [
        "chrome.*--remote-debugging-pipe",   # Playwright default flag
        "playwright.*chromium",              # fallback pattern
    ]

    for pat in patterns:
        # First try pkill which is cheap and available on most Linuxes
        try:
            subprocess.run([
                "pkill", "-9", "-f", pat
            ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            # BusyBox/stripped containers may lack pkill – fall back to ps+kill
            try:
                procs = subprocess.check_output([
                    "ps", "-eo", "pid,cmd"
                ], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                continue  # nothing else we can do

            for line in procs.splitlines():
                if pat in line:
                    try:
                        pid = int(line.split(None, 1)[0])
                        os.kill(pid, signal.SIGKILL)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass

def extract_slug(target: str) -> str:
    m = re.search(r"givesendgo\.com/([^/?#]+)", target, re.I)
    return m.group(1) if m else target.strip()

# Utility ------------------------------------------------------------------

def safe_close_context(ctx):
    """Try to close the BrowserContext, ignoring any errors."""
    try:
        ctx.close()
    except Exception:
        pass

def reg_domain(netloc: str) -> str:
    """Return registered domain (eTLD+1) from a netloc.

    Examples:
        js.stripe.com   -> stripe.com
        api.paypal.com  -> paypal.com
    The function falls back to the last two labels if `tldextract` is missing.
    """
    netloc = netloc.lower().lstrip("www.")
    if _tx is not None:
        t = _tx(netloc, include_psl=True)
        if t.domain and t.suffix:
            return f"{t.domain}.{t.suffix}"
    parts = netloc.split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else netloc

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: pmtproc.py <slug|url>")
        sys.exit(1)

    slug = extract_slug(sys.argv[1])
    har_path = HAR_DIR / f"pmtproc_{slug}_monitor.har"
    print(f"[info] HAR will be saved to: {har_path}")

    matched_urls: list[str] = []

    # Ensure we start from a clean slate
    kill_stale_chromium()

    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=False,
        handle_sigint=False,  # keep the browser alive when user hits CTRL-C
        handle_sigterm=False,
        handle_sighup=False,
    )

    # ------------------------------------------------------------------
    # We rely on a threading.Event to be signalled when either the user closes
    # the browser window or the browser process exits for any other reason.
    stop_event = threading.Event()

    def _on_browser_disconnected() -> None:
        print("[debug] Browser disconnected event.")
        stop_event.set()

    browser.on("disconnected", _on_browser_disconnected)

    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1920, "height": 1080},
        record_har_path=str(har_path),
        record_har_content="omit",  # headers + timings are enough and smaller.
    )

    page = ctx.new_page()

    def capture_request(req):
        if PAYMENT_RE.search(req.url):
            matched_urls.append(req.url)

    page.on("request", capture_request)

    # When the tab closes, flush the HAR immediately then set the stop flag.
    def _on_page_close(_p=None):
        print("[debug] Page closed – flushing HAR …")
        safe_close_context(ctx)
        stop_event.set()

    page.on("close", _on_page_close)

    url = f"https://www.givesendgo.com/{slug}"
    print(f"[info] Opening {url} …")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PWError as e:
        print(f"[error] Couldn't load page: {e}")
    else:
        print("[info] Page loaded. Do whatever you need, then simply close the window.")
        print("[info] Window is ready – close it when you're done … (Press CTRL-C to abort)")

        try:
            # Wait until either the page closes or the browser disconnects.
            # stop_event.wait() lets us sleep while still reacting instantly to the flag.
            while not stop_event.wait(timeout=0.2):
                pass
        except KeyboardInterrupt:
            print("[warn] CTRL-C detected – shutting down …")

    # ---------------------------------------------------------------------
    print("[info] Flushing HAR and cleaning up …")

    # Ignore further CTRL-C during cleanup
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: None)

    # Attempt to close the context (flush HAR) even if the browser has quit.
    safe_close_context(ctx)

    try:
        browser.close()
    except Exception:
        pass
    p.stop()

    # Force-kill any stragglers one last time so no zombie Chromiums remain.
    kill_stale_chromium()

    # Restore SIGINT handler
    signal.signal(signal.SIGINT, original_sigint)

    # ---------------------------------------------------------------------
    if har_path.exists():
        print(f"[info] HAR successfully written → {har_path} ({har_path.stat().st_size} bytes)")
    else:
        print(f"[error] Expected HAR file '{har_path}' was NOT created!")

    # -----------------------------------------------------------------
    # As a fallback, scan the HAR file for matching URLs we might have missed
    # (e.g. those appearing only in cached requests or within headers).
    if har_path.exists():
        try:
            with open(har_path, "r", encoding="utf-8") as fp:
                har = json.load(fp)
            for entry in har.get("log", {}).get("entries", []):
                url = entry.get("request", {}).get("url", "")
                if PAYMENT_RE.search(url):
                    matched_urls.append(url)

                # Scan request & response header values for payment keywords/domains
                for hdr in entry.get("request", {}).get("headers", []):
                    if isinstance(hdr, dict):
                        for extracted in URL_RE.findall(hdr.get("value", "")):
                            if PAYMENT_RE.search(extracted):
                                matched_urls.append(extracted)
                for hdr in entry.get("response", {}).get("headers", []):
                    if isinstance(hdr, dict):
                        for extracted in URL_RE.findall(hdr.get("value", "")):
                            if PAYMENT_RE.search(extracted):
                                matched_urls.append(extracted)
        except Exception as e:
            print(f"[warn] Could not parse HAR for extra matches: {e}")

    if matched_urls:
        # -----------------------------------------------------------------
        # Build a concise domain summary (unique URLs first).
        unique_urls = sorted({u for u in matched_urls if u.startswith("http")})
        domains = [reg_domain(urlparse(u).netloc) for u in unique_urls if urlparse(u).netloc]
        domain_counts = Counter(domains)

        print("\n== Payment-processor domains detected ==")
        for dom, cnt in domain_counts.most_common():
            print(f" • {dom}  ({cnt} request{'s' if cnt != 1 else ''})")

        print("\n== Matching request URLs ==")
        for u in unique_urls:
            print(" •", u)

        sys.exit(0 if har_path.exists() else 1)
    else:
        print("\nNo matching payment URLs captured. HAR saved for manual inspection.")
        sys.exit(0 if har_path.exists() else 1)


if __name__ == "__main__":
    main()
