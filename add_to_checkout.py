"""
kingdomone.store — add-to-cart -> checkout walker
=================================================

Pure `requests`. No Playwright, no headless browser.

Per run:
  1. Builds a fresh, randomized cookie jar (Stripe ids, GA ids, sourcebuster
     attribution) so each run looks like a new visitor.
  2. Warms up the product page so WooCommerce can hand back its own session
     cookies (wc_session_cookie_*, etc.) inside the same `requests.Session`.
  3. POSTs the variation add-to-cart form (multipart/form-data, same fields
     a real browser submits).
  4. Reads /cart/ to confirm the item landed.
  5. GETs /checkout/ and asserts the order-review markup is present.

Run:
    python add_to_checkout.py
    python add_to_checkout.py --runs 3 --delay 4
"""

from __future__ import annotations

import argparse
import random
import re
import string
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote

import requests


# -- Site / product config -----------------------------------------------------

BASE = "https://kingdomone.store"
PRODUCT_URL = f"{BASE}/shop/healthy-things-grow-t-shirt/"
CART_URL = f"{BASE}/cart/"
CHECKOUT_URL = f"{BASE}/checkout/"

PRODUCT_ID = "4217"
VARIATION_ID = "4255"
ATTR_COLOR = "army"
ATTR_SIZE = "m"
QUANTITY = "1"


# -- User-agent pool -----------------------------------------------------------

USER_AGENTS: list[str] = [
    # Mobile Android Chrome
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    # iOS Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    # Desktop Chrome / Edge / Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


# -- Random cookie generator ---------------------------------------------------

def _hex(n: int) -> str:
    return "".join(random.choices(string.hexdigits.lower(), k=n))


def _stripe_id() -> str:
    """Stripe __stripe_mid/__stripe_sid shape:
    8-4-4-4-12 hex followed by 8 trailing hex chars."""
    return f"{_hex(8)}-{_hex(4)}-{_hex(4)}-{_hex(4)}-{_hex(12)}{_hex(8)}"


def _ga_client_id() -> str:
    """_ga value: GA1.1.<random>.<timestamp>"""
    rand = random.randint(10 ** 8, 10 ** 10)
    ts = int(time.time()) - random.randint(0, 60 * 60 * 24 * 30)
    return f"GA1.1.{rand}.{ts}"


def _ga_session_id(measurement_id: str = "XK69GGEQQS") -> tuple[str, str]:
    """Returns (cookie_name, value) for _ga_<MID>."""
    now = int(time.time())
    start = now - random.randint(30, 600)
    val = f"GS2.1.s{start}$o1$g1$t{now}$j{random.randint(5, 40)}$l0$h0"
    return (f"_ga_{measurement_id}", val)


def _sbjs_kv(pairs: Iterable[tuple[str, str]]) -> str:
    """Sourcebuster encodes its multi-field cookies as `k%3Dv%7C%7C%7Ck%3Dv...`."""
    return quote("|||".join(f"{k}={v}" for k, v in pairs), safe="%")


def generate_random_cookies(referer_product: str = PRODUCT_URL) -> dict[str, str]:
    """Build a fresh, plausible cookie jar for a new visitor.

    WooCommerce's own server-side cookies (wc_cart_hash_*, woocommerce_items_in_cart,
    wp_woocommerce_session_*) come back automatically from the warmup GET on the
    product page — don't fabricate those.
    """
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    landing = referer_product

    sbjs_current = _sbjs_kv([
        ("typ", "typein"),
        ("src", "(direct)"),
        ("mdm", "(none)"),
        ("cmp", "(none)"),
        ("cnt", "(none)"),
        ("trm", "(none)"),
        ("id", "(none)"),
        ("plt", "(none)"),
        ("fmt", "(none)"),
        ("tct", "(none)"),
    ])
    sbjs_add = _sbjs_kv([
        ("fd", now_str),
        ("ep", f"{BASE}/"),
        ("rf", landing),
    ])
    sbjs_udata = _sbjs_kv([
        ("vst", "1"),
        ("uip", "(none)"),
        ("uag", random.choice(USER_AGENTS)),
    ])
    ga_name, ga_session = _ga_session_id()

    return {
        "__stripe_mid": _stripe_id(),
        "__stripe_sid": _stripe_id(),
        "sbjs_migrations": "1418474375998%3D1",
        "sbjs_current_add": sbjs_add,
        "sbjs_first_add": sbjs_add,
        "sbjs_current": sbjs_current,
        "sbjs_first": sbjs_current,
        "sbjs_udata": sbjs_udata,
        "_ga": _ga_client_id(),
        "sbjs_session": _sbjs_kv([
            ("pgs", str(random.randint(1, 4))),
            ("cpg", landing),
        ]),
        ga_name: ga_session,
    }


# -- Session builder -----------------------------------------------------------

@dataclass
class Visitor:
    ua: str = field(default_factory=lambda: random.choice(USER_AGENTS))
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.session.cookies.update(generate_random_cookies())
        is_mobile = ("Mobile" in self.ua) or ("iPhone" in self.ua) or ("Android" in self.ua)
        platform = (
            '"Android"' if "Android" in self.ua
            else '"iOS"' if "iPhone" in self.ua
            else '"Windows"' if "Windows" in self.ua
            else '"macOS"' if "Macintosh" in self.ua
            else '"Linux"'
        )
        self.session.headers.update({
            "user-agent": self.ua,
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "max-age=0",
            "upgrade-insecure-requests": "1",
            "sec-ch-ua-mobile": "?1" if is_mobile else "?0",
            "sec-ch-ua-platform": platform,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-user": "?1",
            "sec-fetch-dest": "document",
        })


# -- Flow steps ---------------------------------------------------------------

def _signal(html: str, patterns: list[str]) -> str | None:
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(0)[:120]
    return None


def warmup(v: Visitor) -> requests.Response:
    r = v.session.get(PRODUCT_URL, timeout=25)
    r.raise_for_status()
    return r


def add_to_cart(v: Visitor) -> requests.Response:
    files = {
        "attribute_pa_color": (None, ATTR_COLOR),
        "attribute_pa_size": (None, ATTR_SIZE),
        "quantity": (None, QUANTITY),
        "add-to-cart": (None, PRODUCT_ID),
        "product_id": (None, PRODUCT_ID),
        "variation_id": (None, VARIATION_ID),
    }
    r = v.session.post(
        PRODUCT_URL,
        files=files,
        headers={"referer": PRODUCT_URL, "origin": BASE},
        timeout=25,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r


def view_cart(v: Visitor) -> requests.Response:
    r = v.session.get(CART_URL, headers={"referer": PRODUCT_URL}, timeout=25)
    r.raise_for_status()
    return r


def reach_checkout(v: Visitor) -> requests.Response:
    r = v.session.get(CHECKOUT_URL, headers={"referer": CART_URL}, timeout=25)
    r.raise_for_status()
    return r


# -- Orchestration -------------------------------------------------------------

def run_once(run_id: int = 1) -> bool:
    v = Visitor()
    ua_short = v.ua.split(")")[0].split("(")[-1][:40]
    print(f"\n[run {run_id}] ua={ua_short!r:42}  visitor={uuid.uuid4().hex[:8]}")

    r0 = warmup(v)
    print(f"  [1] warmup       {r0.status_code}  cookies={len(v.session.cookies)}")

    r1 = add_to_cart(v)
    sig = _signal(r1.text, [r"woocommerce-message", r"added to your cart", r"view cart"])
    print(f"  [2] add-to-cart  {r1.status_code}  signal={sig!r}")

    r2 = view_cart(v)
    cart_ok = bool(_signal(r2.text, [r"wc-block-cart-item", r"cart_item", r"product-name"]))
    print(f"  [3] /cart/       {r2.status_code}  has-items={cart_ok}")

    r3 = reach_checkout(v)
    checkout_ok = bool(_signal(
        r3.text,
        [r"billing_first_name", r"wc-block-checkout", r"place_order", r"your order"],
    ))
    print(f"  [4] /checkout/   {r3.status_code}  has-checkout-form={checkout_ok}  url={r3.url}")

    ok = sig is not None and cart_ok and checkout_ok
    print(f"  -> {'OK' if ok else 'FAIL'}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description="kingdomone.store add-to-cart -> checkout walker")
    p.add_argument("--runs", type=int, default=1, help="how many independent visitors to simulate")
    p.add_argument("--delay", type=float, default=2.5, help="seconds between runs (jittered +/- 50%%)")
    args = p.parse_args()

    ok_count = 0
    for i in range(1, args.runs + 1):
        try:
            if run_once(i):
                ok_count += 1
        except requests.RequestException as e:
            print(f"[run {i}] network error: {e}")
        if i < args.runs:
            jitter = args.delay * random.uniform(0.5, 1.5)
            time.sleep(jitter)

    print(f"\nsummary: {ok_count}/{args.runs} runs reached /checkout cleanly")
    return 0 if ok_count == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
