"""Laya local decision-model client — drop-in replacement for JevClient.

Laya (convaiinnovations/laya*) is an open-source non-autoregressive decision
model: it takes a state + typed questions (choice/score/noul) and returns
typed answers with calibrated probabilities — the same contract as Jev's
/systemone endpoint, so this adapter exposes the same `system_one()` shape.

Runs fully locally (no API key, no network per move). The multilingual
checkpoint handles the Chinese rules/piece names. Model weights are fetched
from Hugging Face on first load; set HF_ENDPOINT=https://hf-mirror.com if
huggingface.co is unreachable.
"""

import os


class LayaClient:
    def __init__(self, model="convaiinnovations/laya-multilingual", preload=True):
        # Imported lazily so the server can start without torch unless the
        # laya backend is actually selected.
        if not os.environ.get("HF_ENDPOINT"):
            # hf-mirror.com mirrors huggingface.co for networks that can't
            # reach it directly; harmless if the real hub is reachable.
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        import laya
        self.model_name = model
        self.agent = laya.load(model)
        if preload:
            # One throwaway call so torch kernels/MPS compile before the
            # first real move — cold predict is ~5s, warm is ~0.1-0.5s.
            try:
                self.agent.predict(
                    {"warmup": "ok"},
                    {"w": {"type": "noul", "instructions": "warmup"}},
                )
            except Exception:
                pass

    def system_one(self, state, questions, max_retries=4):
        resp = self.agent.predict(state, questions)
        return {
            "model": self.model_name,
            "answers": resp.get("answers", {}),
            "routing": resp.get("routing"),
            "usage": resp.get("usage", {}),
        }
