"""Thin LLM boundary. LLMs interpret, infer and propose — they always return a validated
pydantic object, never free text that the pipeline has to parse.

Configured in backend/.env (see .env.example):

  OPENAI_API_KEY    set → LLM reasoning on; empty → deterministic rule agents only
  OPENAI_MODEL      model name (default gpt-4.1)
  OPENAI_BASE_URL   optional OpenAI-compatible endpoint (default: OpenAI)

Every call is recorded (see `record_calls`) so it is auditable which answers came from the model.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, TypeVar

import openai
from pydantic import BaseModel, Field, ValidationError

from .. import config  # noqa: F401  (loads backend/.env)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gpt-4.1"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
# A slow model must never stall a draft: after this wall-clock time the agent falls back to its rules.
# (A socket timeout is not enough: routers such as OpenRouter send keep-alive bytes while queueing.)
CALL_TIMEOUT_S = 45
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")

Outcome = Literal["ok", "cached", "refused", "invalid_reply", "timeout", "rate_limited", "api_error", "unreachable", "truncated"]


class LLMCall(BaseModel):
    purpose: str
    model: str  # model requested (OPENAI_MODEL)
    served_model: str | None = None  # model the provider actually ran (routers like OpenRouter pick one)
    outcome: Outcome
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    detail: str = ""
    at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))


_calls: ContextVar[list[LLMCall] | None] = ContextVar("cadpilot_llm_calls", default=None)


@contextmanager
def record_calls() -> Iterator[list[LLMCall]]:
    """Collect every LLM call made inside this block (including LangGraph worker threads)."""
    calls: list[LLMCall] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


class LLM:
    def __init__(self) -> None:
        self.model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        api_key = os.environ.get("OPENAI_API_KEY")
        self._client = (
            openai.OpenAI(
                api_key=api_key,
                base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
                timeout=CALL_TIMEOUT_S,
                max_retries=0,
            )
            if api_key
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def structured(
        self, system: str, prompt: str, schema: type[T], purpose: str,
        memory: dict[str, dict] | None = None, max_tokens: int = 16000,
    ) -> T | None:
        """One structured call. Returns None (caller falls back to rules) on any failure.

        memory: if given, a previous answer for identical inputs is reused, and a new valid answer
        is stored in it.
        """
        if not self._client:
            return None
        key = hashlib.sha256(f"{self.model}\0{schema.__name__}\0{system}\0{prompt}".encode()).hexdigest()[:24]
        if memory is not None and key in memory:
            try:
                cached = schema.model_validate(memory[key]["answer"])
                self._record(LLMCall(purpose=purpose, model=self.model, served_model=memory[key].get("served_model"),
                                     outcome="cached", latency_ms=0, detail="reused answer for unchanged inputs"))
                return cached
            except ValidationError:
                memory.pop(key, None)
        start = time.monotonic()
        result: T | None = None
        served = detail = None
        usage = None
        try:
            future = _pool.submit(
                self._client.chat.completions.parse,
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format=schema,
                max_completion_tokens=max_tokens,
            )
            try:
                completion = future.result(timeout=CALL_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                future.cancel()  # the request thread finishes on its own; its answer is ignored
                raise openai.APITimeoutError(request=None) from None  # type: ignore[arg-type]
            served, usage = completion.model, completion.usage
            message = completion.choices[0].message
            if message.refusal:
                outcome, detail = "refused", message.refusal
            else:
                outcome, result = "ok", message.parsed
        except openai.RateLimitError as exc:
            outcome, detail = "rate_limited", str(exc)
        except openai.APIStatusError as exc:
            outcome, detail = "api_error", f"{exc.status_code}: {exc.message}"
        except openai.APITimeoutError:
            outcome, detail = "timeout", f"no answer within {CALL_TIMEOUT_S}s"
        except openai.APIConnectionError as exc:
            outcome, detail = "unreachable", str(exc)
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
            outcome, detail = "truncated", type(exc).__name__
        except ValidationError as exc:
            # The model ignored the requested schema (e.g. answered in prose); never surface this
            outcome, detail = "invalid_reply", f"reply did not match {schema.__name__}: {exc.errors()[0]['msg']}"

        call = LLMCall(
            purpose=purpose,
            model=self.model,
            served_model=served,
            outcome=outcome,
            latency_ms=int((time.monotonic() - start) * 1000),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            detail=(detail or "")[:500],
        )
        self._record(call)
        if outcome == "ok" and result is not None and memory is not None:
            memory[key] = {"answer": result.model_dump(mode="json"), "served_model": served, "purpose": purpose}
        if outcome == "ok":
            log.info("LLM %s ok (%s, %d ms)", purpose, served, call.latency_ms)
        else:
            log.warning("LLM %s %s, using rules: %s", purpose, outcome, call.detail)
        return result

    @staticmethod
    def _record(call: LLMCall) -> None:
        if (sink := _calls.get()) is not None:
            sink.append(call)


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm


def reset_llm() -> None:
    global _llm
    _llm = None
