"""
Authorized WooCommerce cart-to-checkout smoke test.

This script uses `requests.Session()` to exercise the normal browser flow for an
owned or explicitly permitted WooCommerce store:

1. Start a clean session and warm up the product page so server-issued cookies
   are created naturally.
2. Parse the product form, preserving hidden CSRF/WooCommerce nonce fields.
3. Add one selected product variation to the cart.
4. Refresh WooCommerce cart fragments, open `/cart/`, then open `/checkout/`.
5. Print cart status and the final checkout URL.

It intentionally stops at the checkout page. It does not submit checkout forms,
payment details, CAPTCHA responses, or attempt fraud/anti-bot circumvention.

Playwright is not used or required in this production script. Browser tooling can
be useful during local debugging, but keep it outside this final requests-only
flow.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_BASE_URL = "https://kingdomone.store"
DEFAULT_PRODUCT_PATH = "/shop/healthy-things-grow-t-shirt/"
DEFAULT_PRODUCT_ID = "4217"
DEFAULT_VARIATION_ID = "4255"
DEFAULT_ATTRIBUTES = {
    "attribute_pa_color": "army",
    "attribute_pa_size": "m",
}
DEFAULT_QUANTITY = 1
DEFAULT_TIMEOUT = 25.0

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FlowError(RuntimeError):
    """Raised when the cart-to-checkout flow cannot be completed safely."""


@dataclass
class ProductSelection:
    product_url: str
    product_id: str | None
    variation_id: str | None
    attributes: dict[str, str]
    quantity: int


@dataclass
class ParsedForm:
    action_url: str
    enctype: str
    fields: dict[str, str]
    product_id: str | None
    variations: list[dict[str, Any]]


@dataclass
class CartStatus:
    empty: bool
    has_items: bool
    item_count: int
    woocommerce_cookie_names: list[str]


@dataclass
class CheckoutStatus:
    ready: bool
    url: str
    nonce_fields: list[str]


@dataclass
class FlowResult:
    add_to_cart_status: int
    add_to_cart_url: str
    cart: CartStatus
    checkout: CheckoutStatus


@dataclass
class _FormNode:
    attrs: dict[str, str]
    inputs: list[dict[str, str]]
    buttons: list[dict[str, str]]


class _ProductFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_FormNode] = []
        self._current: _FormNode | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value or "" for name, value in attrs}

        if tag == "form":
            self._current = _FormNode(attrs=attr_map, inputs=[], buttons=[])
            return

        if self._current is None:
            return

        if tag == "input" and attr_map.get("name"):
            self._current.inputs.append(attr_map)
        elif tag == "button" and attr_map.get("name"):
            self._current.buttons.append(attr_map)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _same_origin(left: str, right: str) -> bool:
    a = urlparse(left)
    b = urlparse(right)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def _assert_same_origin(target_url: str, reference_url: str, label: str) -> None:
    if not _same_origin(target_url, reference_url):
        raise FlowError(
            f"{label} attempted to leave the expected origin: {target_url!r}"
        )


def browser_headers(user_agent: str) -> dict[str, str]:
    return {
        "user-agent": user_agent,
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "upgrade-insecure-requests": "1",
        "sec-ch-ua": (
            '"Chromium";v="124", "Google Chrome";v="124", '
            '"Not-A.Brand";v="99"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def navigation_headers(
    *, referer: str | None = None, origin: str | None = None, ajax: bool = False
) -> dict[str, str]:
    if ajax:
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin" if referer else "none",
        }
    else:
        headers = {
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin" if referer else "none",
            "sec-fetch-user": "?1",
        }

    if referer:
        headers["referer"] = referer
    if origin:
        headers["origin"] = origin
    return headers


def build_session(
    *, user_agent: str, retries: int, backoff: float, cookie_jar: Path | None
) -> tuple[requests.Session, MozillaCookieJar | None]:
    session = requests.Session()
    session.headers.update(browser_headers(user_agent))

    retry_config = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_config)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    jar: MozillaCookieJar | None = None
    if cookie_jar:
        jar = MozillaCookieJar(str(cookie_jar))
        if cookie_jar.exists():
            jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = jar

    return session, jar


def save_cookie_jar(jar: MozillaCookieJar | None, path: Path | None) -> None:
    if jar is None or path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)


def parse_product_form(html_text: str, product_page_url: str) -> ParsedForm:
    parser = _ProductFormParser()
    parser.feed(html_text)

    if not parser.forms:
        raise FlowError("no HTML forms were found on the product page")

    form = max(parser.forms, key=_cart_form_score)
    if _cart_form_score(form) == 0:
        raise FlowError(
            "no WooCommerce add-to-cart form was found on the product page"
        )

    action = form.attrs.get("action") or product_page_url
    action_url = urljoin(product_page_url, action)
    _assert_same_origin(action_url, product_page_url, "product form action")

    fields = _extract_relevant_form_fields(form)
    product_id = (
        form.attrs.get("data-product_id")
        or fields.get("product_id")
        or fields.get("add-to-cart")
    )

    return ParsedForm(
        action_url=action_url,
        enctype=form.attrs.get("enctype", "application/x-www-form-urlencoded").lower(),
        fields=fields,
        product_id=product_id or None,
        variations=_parse_variation_json(form.attrs.get("data-product_variations", "")),
    )


def _cart_form_score(form: _FormNode) -> int:
    score = 0
    class_attr = form.attrs.get("class", "")
    field_names = {
        control.get("name", "")
        for control in [*form.inputs, *form.buttons]
        if control.get("name")
    }

    if "variations_form" in class_attr:
        score += 100
    if re.search(r"\bcart\b", class_attr):
        score += 50
    if "add-to-cart" in field_names:
        score += 25
    if "product_id" in field_names:
        score += 20
    if form.attrs.get("data-product_id"):
        score += 20
    return score


def _extract_relevant_form_fields(form: _FormNode) -> dict[str, str]:
    fields: dict[str, str] = {}

    for control in form.inputs:
        name = control.get("name")
        if not name:
            continue
        control_type = control.get("type", "text").lower()
        if (
            control_type == "hidden"
            or "nonce" in name.lower()
            or name.startswith("_wp")
            or name in {"add-to-cart", "product_id", "variation_id"}
        ):
            fields[name] = control.get("value", "")

    for button in form.buttons:
        name = button.get("name")
        if name in {"add-to-cart", "product_id"}:
            fields[name] = button.get("value", "")

    return fields


def _parse_variation_json(raw: str) -> list[dict[str, Any]]:
    if not raw or raw.lower() == "false":
        return []

    try:
        parsed = json.loads(unescape(raw))
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def resolve_variation_id(
    parsed_form: ParsedForm, selection: ProductSelection
) -> str | None:
    if selection.variation_id:
        return selection.variation_id

    if not selection.attributes:
        return None

    for variation in parsed_form.variations:
        attrs = variation.get("attributes")
        if not isinstance(attrs, dict):
            continue
        if _attributes_match(attrs, selection.attributes):
            if variation.get("is_in_stock") is False:
                raise FlowError("matched variation is present but marked out of stock")
            variation_id = variation.get("variation_id")
            return str(variation_id) if variation_id else None

    raise FlowError(
        "could not resolve a variation_id from the selected attributes; pass "
        "--variation-id explicitly or verify the attribute values"
    )


def _attributes_match(page_attrs: dict[str, Any], requested: dict[str, str]) -> bool:
    for name, expected in requested.items():
        actual = str(page_attrs.get(name, "")).strip().lower()
        expected_normalized = expected.strip().lower()
        if actual and actual != expected_normalized:
            return False
    return True


def build_add_to_cart_payload(
    parsed_form: ParsedForm, selection: ProductSelection
) -> dict[str, str]:
    product_id = selection.product_id or parsed_form.product_id
    if not product_id:
        raise FlowError("could not resolve product_id from CLI args or page form")

    variation_id = resolve_variation_id(parsed_form, selection)
    if selection.attributes and not variation_id:
        raise FlowError(
            "a product variation was selected, but no variation_id was resolved"
        )

    payload = dict(parsed_form.fields)
    payload.update(selection.attributes)
    payload["quantity"] = str(selection.quantity)
    payload["add-to-cart"] = str(product_id)
    payload["product_id"] = str(product_id)
    if variation_id:
        payload["variation_id"] = str(variation_id)
    return payload


class WooCommerceCartFlow:
    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str,
        cart_path: str,
        checkout_path: str,
        selection: ProductSelection,
        timeout: float,
        verbose: bool = False,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.cart_url = urljoin(f"{self.base_url}/", cart_path.lstrip("/"))
        self.checkout_url = urljoin(f"{self.base_url}/", checkout_path.lstrip("/"))
        self.selection = selection
        self.timeout = timeout
        self.verbose = verbose

        _assert_same_origin(self.selection.product_url, self.base_url, "product URL")
        _assert_same_origin(self.cart_url, self.base_url, "cart URL")
        _assert_same_origin(self.checkout_url, self.base_url, "checkout URL")

    def dry_run(self) -> dict[str, Any]:
        product_response = self._get_product_page()
        parsed_form = parse_product_form(product_response.text, product_response.url)
        payload = build_add_to_cart_payload(parsed_form, self.selection)
        return {
            "product_page_url": product_response.url,
            "form_action_url": parsed_form.action_url,
            "form_enctype": parsed_form.enctype,
            "payload": _redacted_payload(payload),
            "nonce_fields": _nonce_field_names(payload),
        }

    def run(self) -> FlowResult:
        product_response = self._get_product_page()
        parsed_form = parse_product_form(product_response.text, product_response.url)
        payload = build_add_to_cart_payload(parsed_form, self.selection)

        add_response = self._post_add_to_cart(
            parsed_form=parsed_form,
            payload=payload,
            referer=product_response.url,
        )
        self._refresh_cart_fragments(referer=add_response.url or product_response.url)

        cart_response = self._get_cart(referer=add_response.url or product_response.url)
        cart_status = self._cart_status(cart_response.text)
        if not cart_status.has_items:
            raise FlowError("cart page loaded, but no cart item was detected")

        checkout_response = self._get_checkout(referer=cart_response.url)
        checkout_status = self._checkout_status(checkout_response)
        if not checkout_status.ready:
            raise FlowError("checkout page loaded, but checkout form/block was not detected")

        return FlowResult(
            add_to_cart_status=add_response.status_code,
            add_to_cart_url=add_response.url,
            cart=cart_status,
            checkout=checkout_status,
        )

    def _get_product_page(self) -> requests.Response:
        response = self.session.get(
            self.selection.product_url,
            headers=navigation_headers(),
            timeout=self.timeout,
            allow_redirects=True,
        )
        return self._checked_response(response, "product page warmup")

    def _post_add_to_cart(
        self, *, parsed_form: ParsedForm, payload: dict[str, str], referer: str
    ) -> requests.Response:
        headers = navigation_headers(referer=referer, origin=self.base_url)

        if "multipart/form-data" in parsed_form.enctype:
            response = self.session.post(
                parsed_form.action_url,
                files={name: (None, value) for name, value in payload.items()},
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
        else:
            response = self.session.post(
                parsed_form.action_url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
            )

        return self._checked_response(response, "add-to-cart POST")

    def _refresh_cart_fragments(self, *, referer: str) -> None:
        ajax_url = urljoin(f"{self.base_url}/", "/?wc-ajax=get_refreshed_fragments")
        try:
            response = self.session.post(
                ajax_url,
                data={},
                headers=navigation_headers(referer=referer, origin=self.base_url, ajax=True),
                timeout=self.timeout,
                allow_redirects=True,
            )
            if self.verbose:
                print(f"cart fragments: HTTP {response.status_code}")
        except requests.RequestException as exc:
            if self.verbose:
                print(f"cart fragments: skipped after network error: {exc}")

    def _get_cart(self, *, referer: str) -> requests.Response:
        response = self.session.get(
            self.cart_url,
            headers=navigation_headers(referer=referer),
            timeout=self.timeout,
            allow_redirects=True,
        )
        return self._checked_response(response, "cart page")

    def _get_checkout(self, *, referer: str) -> requests.Response:
        response = self.session.get(
            self.checkout_url,
            headers=navigation_headers(referer=referer),
            timeout=self.timeout,
            allow_redirects=True,
        )
        return self._checked_response(response, "checkout page")

    def _checked_response(
        self, response: requests.Response, label: str
    ) -> requests.Response:
        _assert_same_origin(response.url, self.base_url, label)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise FlowError(
                f"{label} returned HTTP {response.status_code}: {response.url}"
            ) from exc
        return response

    def _cart_status(self, html_text: str) -> CartStatus:
        empty = bool(
            re.search(
                r"cart-empty|your cart is currently empty|wc-empty-cart-message",
                html_text,
                re.IGNORECASE,
            )
        )
        item_count = len(
            re.findall(
                r"<(?:tr|li|div)\b[^>]*class=[\"'][^\"']*"
                r"(?:woocommerce-cart-form__cart-item|wc-block-cart-item|\bcart_item\b)"
                r"[^\"']*[\"']",
                html_text,
                re.IGNORECASE,
            )
        )
        product_markers = [
            marker
            for marker in [self.selection.product_id, self.selection.variation_id]
            if marker
        ]
        has_product_marker = any(marker in html_text for marker in product_markers)
        has_checkout_link = bool(
            re.search(
                r"proceed-to-checkout|checkout-button|wc-block-cart__submit",
                html_text,
                re.I,
            )
        )
        has_items = not empty and (item_count > 0 or has_product_marker or has_checkout_link)

        woo_cookie_names = sorted(
            cookie.name
            for cookie in self.session.cookies
            if "woocommerce" in cookie.name.lower() or cookie.name.lower().startswith("wc_")
        )
        return CartStatus(
            empty=empty,
            has_items=has_items,
            item_count=item_count,
            woocommerce_cookie_names=woo_cookie_names,
        )

    def _checkout_status(self, response: requests.Response) -> CheckoutStatus:
        html_text = response.text
        empty = bool(
            re.search(
                r"cart-empty|your cart is currently empty|return to shop",
                html_text,
                re.IGNORECASE,
            )
        )
        ready = not empty and bool(
            re.search(
                r"woocommerce-checkout|wc-block-checkout|billing_first_name|"
                r"woocommerce-process-checkout-nonce|place_order|your order",
                html_text,
                re.IGNORECASE,
            )
        )
        nonce_fields = sorted(
            set(re.findall(r'name=["\']([^"\']*nonce[^"\']*)["\']', html_text, re.I))
        )
        return CheckoutStatus(ready=ready, url=response.url, nonce_fields=nonce_fields)


def _nonce_field_names(payload: dict[str, str]) -> list[str]:
    return sorted(
        name for name in payload if "nonce" in name.lower() or name.startswith("_wp")
    )


def _redacted_payload(payload: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for name, value in payload.items():
        if "nonce" in name.lower() or name.startswith("_wp"):
            redacted[name] = "<present>"
        else:
            redacted[name] = value
    return redacted


def parse_attribute(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "attributes must be KEY=VALUE, e.g. attribute_pa_size=m"
        )
    key, attr_value = value.split("=", 1)
    key = key.strip()
    attr_value = attr_value.strip()
    if not key or not attr_value:
        raise argparse.ArgumentTypeError("attributes must include both KEY and VALUE")
    return key, attr_value


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Authorized requests-only WooCommerce product variation add-to-cart "
            "and checkout-page smoke test."
        )
    )
    parser.add_argument(
        "--authorized",
        action="store_true",
        help=(
            "required for non-dry-run use; confirms you own or have permission "
            "to test the store"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and parse the product form, but do not add anything to the cart",
    )
    parser.add_argument(
        "--base-url", default=None, help=f"store origin, default: {DEFAULT_BASE_URL}"
    )
    parser.add_argument("--product-url", default=None, help="absolute product page URL")
    parser.add_argument(
        "--product-path",
        default=DEFAULT_PRODUCT_PATH,
        help=f"product path when --product-url is omitted, default: {DEFAULT_PRODUCT_PATH}",
    )
    parser.add_argument("--cart-path", default="/cart/", help="cart path, default: /cart/")
    parser.add_argument(
        "--checkout-path", default="/checkout/", help="checkout path, default: /checkout/"
    )
    parser.add_argument(
        "--product-id",
        default=None,
        help="WooCommerce product ID; inferred from the page when omitted",
    )
    parser.add_argument(
        "--variation-id",
        default=None,
        help="WooCommerce variation ID; inferred from page JSON when omitted",
    )
    parser.add_argument(
        "--attribute",
        action="append",
        type=parse_attribute,
        default=[],
        metavar="KEY=VALUE",
        help=(
            "selected variation attribute; repeatable. The built-in kingdomone.store "
            "target defaults to attribute_pa_color=army and attribute_pa_size=m"
        ),
    )
    parser.add_argument("--quantity", type=int, default=DEFAULT_QUANTITY)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=2, help="safe GET/HEAD retry count")
    parser.add_argument("--backoff", type=float, default=0.4, help="retry backoff factor")
    parser.add_argument(
        "--cookie-jar", type=Path, default=None, help="optional Mozilla cookie jar path"
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    if args.quantity < 1:
        parser.error("--quantity must be >= 1")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.retries < 0:
        parser.error("--retries must be >= 0")
    if args.backoff < 0:
        parser.error("--backoff must be >= 0")
    if not args.authorized and not args.dry_run:
        parser.error("--authorized is required before making cart changes")
    return args


def build_selection(args: argparse.Namespace) -> tuple[str, ProductSelection]:
    base_url = args.base_url or DEFAULT_BASE_URL
    if args.product_url:
        product_url = args.product_url
        if args.base_url is None:
            base_url = _origin(product_url)
    else:
        product_url = urljoin(f"{base_url.rstrip('/')}/", args.product_path.lstrip("/"))

    default_product_url = urljoin(
        f"{DEFAULT_BASE_URL.rstrip('/')}/", DEFAULT_PRODUCT_PATH.lstrip("/")
    )
    uses_builtin_default_product = product_url.rstrip("/") == default_product_url.rstrip("/")

    attributes = (
        dict(args.attribute)
        if args.attribute
        else dict(DEFAULT_ATTRIBUTES)
        if uses_builtin_default_product
        else {}
    )
    product_id = args.product_id or (
        DEFAULT_PRODUCT_ID if uses_builtin_default_product else None
    )
    variation_id = args.variation_id or (
        DEFAULT_VARIATION_ID if uses_builtin_default_product else None
    )
    return base_url, ProductSelection(
        product_url=product_url,
        product_id=product_id,
        variation_id=variation_id,
        attributes=attributes,
        quantity=args.quantity,
    )


def print_dry_run(plan: dict[str, Any]) -> None:
    print("dry-run: product form resolved")
    print(f"  product page: {plan['product_page_url']}")
    print(f"  form action:  {plan['form_action_url']}")
    print(f"  enctype:      {plan['form_enctype']}")
    print(f"  nonce fields: {', '.join(plan['nonce_fields']) or 'none detected'}")
    print("  payload:")
    for name, value in plan["payload"].items():
        print(f"    {name}={value}")


def print_result(result: FlowResult) -> None:
    cart_cookie_summary = ", ".join(result.cart.woocommerce_cookie_names) or "none"
    nonce_summary = ", ".join(result.checkout.nonce_fields) or "none detected"

    print(f"add-to-cart: HTTP {result.add_to_cart_status} -> {result.add_to_cart_url}")
    print(
        "cart status: "
        f"has_items={result.cart.has_items} "
        f"empty={result.cart.empty} "
        f"item_count={result.cart.item_count} "
        f"woocommerce_cookies=[{cart_cookie_summary}]"
    )
    print(f"checkout URL: {result.checkout.url}")
    print(f"checkout nonce fields: {nonce_summary}")
    print("stopped at checkout page; no payment or checkout submission was attempted")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    base_url, selection = build_selection(args)
    session, cookie_jar = build_session(
        user_agent=args.user_agent,
        retries=args.retries,
        backoff=args.backoff,
        cookie_jar=args.cookie_jar,
    )

    flow = WooCommerceCartFlow(
        session=session,
        base_url=base_url,
        cart_path=args.cart_path,
        checkout_path=args.checkout_path,
        selection=selection,
        timeout=args.timeout,
        verbose=args.verbose,
    )

    try:
        if args.dry_run:
            print_dry_run(flow.dry_run())
            return 0

        print_result(flow.run())
        return 0
    except (FlowError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        save_cookie_jar(cookie_jar, args.cookie_jar)


if __name__ == "__main__":
    raise SystemExit(main())
