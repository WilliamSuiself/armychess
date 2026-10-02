"""Laya local decision-model client — drop-in replacement for JevClient.

Laya (convaiinnovations/laya*) is an open-source, non-autoregressive
decision model: it takes a state + typed questions (choice/score/noul)
and returns typed answers with calibrated probabilities — the same
contract as Jev's /systemone endpoint, so this exposes the same
`system_one()` shape and can be swapped in transparently.

Runs fully locally (no API key, no network per move). Weights are
fetched from Hugging Face on first load; set HF_ENDPOINT=
https://hf-mirror.com if huggingface.co is unreachable.
"""

import os


class LayaClient:
    def __init__(self, model="convaiinnovations/laya-multilingual", preload=True):
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        import laya
        self.model_name = model
        self.agent = laya.load(model)
        if preload:
            try:
                self.agent.predict(
                    {"warmup": "ok"},
                    {"w": {"type": "noul", "instructions": "warmup"}},
                )
            except Exception:
                pass

    def system_one(self, state, questions, max_retries=4):
        import sys, time as _t
        _t0 = _t.time()
        resp = self.agent.predict(state, questions)
        print(f"[laya] predict took {_t.time()-_t0:.2f}s", file=sys.stderr)
        return {
            "model": self.model_name,
            "answers": resp.get("answers", {}),
            "routing": resp.get("routing"),
            "usage": resp.get("usage", {}),
        }
