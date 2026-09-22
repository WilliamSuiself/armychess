"""Jev API client — minimal wrapper for knox.chat systemone endpoint."""

import os
import sys
import time
import requests


class JevClient:
    def __init__(self, api_key=None, base_url=None, model="jev-latest", timeout=30):
        self.api_key = api_key or os.environ.get("KNOX_API_KEY")
        self.base_url = base_url or os.environ.get("KNOX_BASE_URL", "https://api.knox.chat/v1")
        self.model = model
        self.timeout = timeout
        if not self.api_key:
            raise ValueError("KNOX_API_KEY not set (env or api_key arg)")

    def system_one(self, state, questions, max_retries=4):
        url = f"{self.base_url}/systemone"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "state": state if not isinstance(state, str) else state,
            "questions": questions,
        }
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                # Retry on transient upstream / rate limit
                if r.status_code in (429, 502, 503, 529):
                    backoff = min(8, 2 ** attempt)
                    print(f"[jev] status={r.status_code}, retry in {backoff}s (attempt {attempt+1}/{max_retries+1})", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                if not r.ok:
                    raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:200]}")
                return r.json()
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    backoff = min(8, 2 ** attempt)
                    print(f"[jev] exception={e!r}, retry in {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
        raise last_err or RuntimeError("Jev call failed after retries")