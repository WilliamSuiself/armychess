"""Jev API client — minimal wrapper for knox.chat systemone endpoint.

Same contract as the army-chess project this was lifted from: POST a
`state` (arbitrary JSON) + `questions` (typed choice/score/noul dict),
get back typed `answers`.

The API key is hardcoded as a fallback per project request (this is a
local demo app, not a service with its own users) — it's still
overridable via KNOX_API_KEY/KNOX_BASE_URL env vars if you want to swap
it out later.
"""

import os
import sys
import time
import requests

# Hardcoded fallback so the game works out of the box with no .env setup.
DEFAULT_KNOX_API_KEY = os.environ.get("KNOX_API_KEY")
DEFAULT_KNOX_BASE_URL = "https://api.knox.chat/v1"


class JevClient:
    # Measured baseline is ~0.9s median / ~1s p90 for this game's requests, so 30s was a
    # very conservative timeout — it just meant a rare upstream stall held up a piece for
    # the full 30s before the retry kicked in. 12s is still >10x the normal p90 while
    # cutting that worst-case stall roughly in half.
    def __init__(self, api_key=None, base_url=None, model="jev-latest", timeout=12):
        self.api_key = api_key or os.environ.get("KNOX_API_KEY")
        self.base_url = base_url or os.environ.get("KNOX_BASE_URL", DEFAULT_KNOX_BASE_URL)
        self.model = model
        self.timeout = timeout
        if not self.api_key:
            raise ValueError("KNOX_API_KEY not set (env, api_key arg, or hardcoded default)")

    def system_one(self, state, questions, max_retries=4):
        url = f"{self.base_url}/systemone"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "state": state,
            "questions": questions,
        }
        last_err = None
        for attempt in range(max_retries + 1):
            t0 = time.time()
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                call_s = time.time() - t0
                if r.status_code in (429, 502, 503, 529):
                    backoff = min(8, 2 ** attempt)
                    print(f"[jev] status={r.status_code} after {call_s:.1f}s, retry in "
                          f"{backoff}s (attempt {attempt+1}/{max_retries+1})", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                if not r.ok:
                    raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:200]}")
                if call_s > 5:
                    print(f"[jev] slow call: {call_s:.1f}s (attempt {attempt+1}/"
                          f"{max_retries+1})", file=sys.stderr)
                return r.json()
            except Exception as e:
                call_s = time.time() - t0
                last_err = e
                if attempt < max_retries:
                    backoff = min(8, 2 ** attempt)
                    print(f"[jev] exception after {call_s:.1f}s: {e!r}, retry in {backoff}s "
                          f"(attempt {attempt+1}/{max_retries+1})", file=sys.stderr)
                    time.sleep(backoff)
        raise last_err or RuntimeError("Jev call failed after retries")
