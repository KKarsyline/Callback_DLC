"""Portable host binding for the release's batch, shared-query and cache methods."""
import asyncio
import json
import logging
import math
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class EmbeddingEngine:
    def __init__(self, client, model, *, max_chars=6000, query_instruction="",
                 document_instruction="", provider_order=None):
        self.client, self.model = client, model
        self.max_chars = max_chars
        self.query_instruction = query_instruction
        self.document_instruction = document_instruction
        self.enabled = bool(client and model)
        self.provider_routing = None
        hostname = urlsplit(str(getattr(client, "base_url", ""))).hostname
        if provider_order is not None and hostname == "openrouter.ai":
            if not isinstance(provider_order, list) or not provider_order:
                raise ValueError("provider_order requires a nonempty list")
            names = [str(name).lower() for name in provider_order]
            if set(names) - {"siliconflow", "nebius"} or len(names) != len(set(names)):
                raise ValueError("unsupported or repeated provider")
            self.provider_routing = {"order": names, "allow_fallbacks": False}
        self._query_tasks = {}
        self._query_cache = {}
        self._query_cache_ttl = 8.0

    def _prepare_embedding_input(self, text, *, kind):
        instruction = self.query_instruction if kind == "query" else self.document_instruction
        return f"Instruct: {instruction}\n{kind.title()}: {text}" if instruction else str(text or "")

    def _query_key(self, text):
        prepared = self._prepare_embedding_input(text, kind="query")[:self.max_chars]
        return self.model, "query", prepared, self.max_chars, json.dumps(self.provider_routing, sort_keys=True)

    async def _request_embedding(self, prepared):
        args = {"model": self.model, "input": prepared, "encoding_format": "float"}
        if self.provider_routing:
            args["extra_body"] = {"provider": self.provider_routing}
        try:
            response = await self.client.embeddings.create(**args)
            return list(response.data[0].embedding or []) if response.data else []
        except Exception as exc:
            logger.warning("Embedding unavailable: %s", type(exc).__name__)
            return []

    async def generate_query_embeddings(self, texts: list[str]) -> list[list[float] | None]:
        """Embed up to two independent query directions in one provider request.

        Results remain aligned with the supplied directions. Provider indexes are
        treated as an explicit scatter map; missing, repeated, malformed, or
        non-finite results become None rather than shifting another vector.
        """
        texts=[str(text or "") for text in texts][:2]
        if not texts:return []
        prepared=[self._prepare_embedding_input(text,kind="query")[:self.max_chars] for text in texts]
        output=[None]*len(prepared)
        keys=[(self.model,"query",item,self.max_chars,json.dumps(self.provider_routing,sort_keys=True)) for item in prepared]
        now=asyncio.get_running_loop().time()
        pending=[];pending_positions=[];unique={}
        for index,(key,item) in enumerate(zip(keys,prepared)):
            cached=self._query_cache.get(key)
            if cached and cached[0]>now:
                output[index]=list(cached[1]);continue
            if cached:self._query_cache.pop(key,None)
            if key in unique:
                continue
            unique[key]=len(pending);pending.append(item);pending_positions.append(index)
        if pending:
            try:
                kwargs={"model":self.model,"input":pending,"encoding_format":"float"}
                if self.provider_routing:kwargs["extra_body"]={"provider":self.provider_routing}
                client=self.client.with_options(max_retries=0) if hasattr(self.client,"with_options") else self.client
                response=await client.embeddings.create(**kwargs)
                scattered=[None]*len(pending);counts=[0]*len(pending)
                for datum in (getattr(response,"data",None) or []):
                    try:
                        index=getattr(datum,"index",None)
                        if index is None and isinstance(datum,dict):index=datum.get("index")
                        if type(index) is not int:continue
                        embedding=getattr(datum,"embedding",None)
                        if embedding is None and isinstance(datum,dict):embedding=datum.get("embedding")
                        if not 0<=index<len(pending):continue
                        counts[index]+=1
                        if counts[index]!=1 or not isinstance(embedding,(list,tuple)) or not embedding:continue
                        vector=[float(value) for value in embedding]
                        if not all(math.isfinite(value) for value in vector):continue
                        scattered[index]=vector
                    except (TypeError,ValueError,OverflowError):
                        continue
                stamp=asyncio.get_running_loop().time()
                self._query_cache={k:v for k,v in self._query_cache.items() if v[0]>stamp}
                for offset,index in enumerate(pending_positions):
                    key=keys[index];vector=scattered[offset] if counts[offset]==1 else None
                    output[index]=list(vector) if vector is not None else None
                    if vector is not None:
                        if len(self._query_cache)>=128:self._query_cache.pop(next(iter(self._query_cache)))
                        self._query_cache[key]=(stamp+self._query_cache_ttl,list(vector))
            except Exception as e:
                logger.warning("Batch embedding API call failed: %s",type(e).__name__)
        # Duplicate prepared directions share the aligned result of their first copy.
        for index,key in enumerate(keys):
            if output[index] is None and key in unique:
                source=pending_positions[unique[key]]
                if source!=index:output[index]=list(output[source]) if output[source] is not None else None
        return output

    async def _generate_embedding(self, text: str, *, kind: str = "document") -> list[float]:
        """Generate vectors; concurrent identical query calls share one request."""
        prepared = self._prepare_embedding_input(text, kind=kind)[: self.max_chars]
        if kind != "query":
            return await self._request_embedding(prepared)
        key = self._query_key(text)
        now = asyncio.get_running_loop().time()
        cached = self._query_cache.get(key)
        if cached and cached[0] > now:
            return list(cached[1])
        if cached:
            self._query_cache.pop(key, None)
        task = self._query_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._request_embedding(prepared))
            self._query_tasks[key] = task
            def complete(future):
                if self._query_tasks.get(key) is future:
                    self._query_tasks.pop(key,None)
                if future.cancelled() or future.exception(): return
                result = future.result()
                if result:
                    stamp = asyncio.get_running_loop().time()
                    self._query_cache = {k:v for k,v in self._query_cache.items() if v[0]>stamp}
                    if len(self._query_cache)>=128:
                        self._query_cache.pop(next(iter(self._query_cache)))
                    self._query_cache[key] = (stamp+self._query_cache_ttl,result)
            task.add_done_callback(complete)
        return list(await asyncio.shield(task))

    def peek_query_embedding(self, text: str) -> list[float] | None:
        """Return only an unexpired exact-literal query vector; never starts I/O."""
        key = self._query_key(text)
        cached = self._query_cache.get(key)
        if cached and cached[0] > asyncio.get_running_loop().time():
            return list(cached[1])
        return None

    async def wait_query_embedding(self, text: str, *, timeout_ms: int = 5) -> list[float] | None:
        """Join an already-running literal query briefly, without creating one."""
        loop = asyncio.get_running_loop()
        deadline = loop.time()+max(0,timeout_ms)/1000
        registration = min(deadline,loop.time()+.25)
        key=self._query_key(text)
        while True:
            cached=self.peek_query_embedding(text)
            if cached is not None:return cached
            task=self._query_tasks.get(key)
            if task is not None:break
            if loop.time()>=registration:return None
            await asyncio.sleep(.01)
        try:
            result=await asyncio.wait_for(asyncio.shield(task),max(0,deadline-loop.time()))
            return list(result) if result else None
        except asyncio.TimeoutError:
            return None
