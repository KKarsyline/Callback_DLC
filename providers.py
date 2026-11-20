"""OpenAI-compatible clients; host owns credentials, models, and shutdown."""
import httpx
from urllib.parse import urlsplit


class RecallModel:
    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.api_available = bool(client and model)

    def _completion_options(self, *, max_tokens, temperature):
        return {"max_tokens": max_tokens, "temperature": temperature}


class GatewayJudge:
    def __init__(self, client: httpx.AsyncClient, model: str, *, budget_db=None, trusted_cache_judge=False):
        self._client = client
        self._model = model
        self.budget_db = budget_db
        if trusted_cache_judge and urlsplit(str(client.base_url)).hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("parallel cached-evidence judgment requires a trusted local endpoint")
        self.trusted_cache_judge = trusted_cache_judge

    async def _post(self, path, *, json, _retries=0):
        payload = dict(json)
        if self._client.base_url.host != "openrouter.ai":
            payload.pop("reasoning", None)
        return await self._client.post(path.lstrip("/"), json=payload)
