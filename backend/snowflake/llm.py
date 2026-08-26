"""FROZEN filename at tag contracts-v1. Body owned by Card 1 (Snowflake platform).

CortexLLMClient implements backend.contracts.ports.LLMPort against
SNOWFLAKE.CORTEX.COMPLETE. Single provider, no fallback - see
plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 3.5 and
plan-v2/00-SHARED-CONTRACTS.md section 2.2.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time

from backend.app.llm.cache import cache_key, get_default_cache, is_cacheable_call_site
from backend.app.llm.json_repair import build_repair_messages, try_parse_json
from backend.app.llm.pricing import ModelPriceRow, compute_cost_usd
from backend.app.llm.routing import model_for_call_site
from backend.app.llm.tokenizer import estimate_tokens
from backend.contracts.models import CallSite, ChatResult, LedgerEvent, Message, TokenUsage
from backend.snowflake.session import get_session, snowflake_available

logger = logging.getLogger("neulit.snowflake.llm")

_TIMEOUT_SECONDS = 20
_MAX_RETRIES = 3
_DEFAULT_MODEL = "claude-sonnet-4-5"


def _active_model() -> str:
    """Legacy single-model resolution, kept for health() (which reports
    readiness independent of any particular call_site) and as the ultimate
    fallback semantics documented in backend/app/llm/routing.py.
    """
    return os.environ.get("SNOWFLAKE_CORTEX_MODEL", _DEFAULT_MODEL)


class CortexLLMClient:
    """Single-provider LLM client: SNOWFLAKE.CORTEX.COMPLETE only.

    Every chat() call writes exactly one LedgerEvent, via the injected
    ledger, in a finally block - across success, retry-then-success, and
    total-failure/degraded paths.
    """

    def __init__(self, ledger=None) -> None:
        # `ledger` is optional/injectable for tests; production resolves it
        # lazily from the registry so CortexLLMClient() (as registry._load
        # constructs it, with zero args) still works.
        self._ledger = ledger
        self._pricing_cache: dict[str, ModelPriceRow] | None = None

    # -- ledger resolution -------------------------------------------------
    def _resolve_ledger(self):
        if self._ledger is not None:
            return self._ledger
        from backend.contracts.registry import get_services

        return get_services().ledger

    # -- pricing -------------------------------------------------------
    def _load_pricing(self) -> dict[str, ModelPriceRow]:
        if self._pricing_cache is not None:
            return self._pricing_cache
        pricing: dict[str, ModelPriceRow] = {}
        session = get_session()
        if session is not None:
            try:
                rows = session.sql(
                    "SELECT MODEL, CREDITS_PER_MTOK_IN, CREDITS_PER_MTOK_OUT, USD_PER_CREDIT "
                    "FROM NEULIT.CORE.MODEL_PRICING"
                ).collect()
                for row in rows:
                    pricing[row["MODEL"]] = ModelPriceRow(
                        model=row["MODEL"],
                        credits_per_mtok_in=float(row["CREDITS_PER_MTOK_IN"]),
                        credits_per_mtok_out=float(row["CREDITS_PER_MTOK_OUT"]),
                        usd_per_credit=float(row["USD_PER_CREDIT"]),
                    )
            except Exception:
                logger.exception("llm: failed to load MODEL_PRICING")
        self._pricing_cache = pricing
        return pricing

    # -- Cortex call -----------------------------------------------------
    def _call_complete(self, prompt_payload, model: str, options: dict):
        """Runs SNOWFLAKE.CORTEX.COMPLETE via Snowpark. Raises on any
        transport/Snowflake-side failure; caller handles retry/backoff.
        """
        session = get_session()
        if session is None:
            raise RuntimeError("snowflake session unavailable")

        result = session.sql(
            "SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?), PARSE_JSON(?)) AS RESP",
            params=[model, json.dumps(prompt_payload), json.dumps(options)],
        ).collect(statement_params={"STATEMENT_TIMEOUT_IN_SECONDS": str(_TIMEOUT_SECONDS)})
        return result[0]["RESP"]

    def chat(
        self,
        messages: list[Message],
        *,
        call_site: CallSite,
        request_id: str,
        session_id: str,
        user_id: str,
        json_schema: dict | None = None,
        max_output_tokens: int = 1024,
    ) -> ChatResult:
        start = time.monotonic()
        # Phase E: per-call-site model routing -- cheap tier for
        # relevance_check/hyde, strong tier for summary/citation_check/
        # refine/memory_distill. See backend/app/llm/routing.py for the
        # env-var override priority and default model choices. Everything
        # downstream (pricing lookup, TokenUsage.model, LedgerEvent) uses
        # this resolved `model`, so cost/ledger naturally reflect whichever
        # model actually served this call.
        model = model_for_call_site(call_site)
        degraded = False
        error: str | None = None
        content = ""
        prompt_tokens = 0
        completion_tokens = 0
        estimated = False
        cache_hit = False

        # Prompt cache: scoped to hyde/relevance_check only (Phase B.3).
        # Cache hit returns the cached ChatResult without ever calling
        # Cortex COMPLETE; the LedgerEvent written for this call in the
        # `finally` block below still records token/cost as zero (a hit
        # cost nothing), tracked separately in the module-level CacheStats
        # rather than on the frozen LedgerEvent.
        cache = get_default_cache()
        cache_key_val: str | None = None
        if is_cacheable_call_site(call_site):
            cache_key_val = cache_key(call_site, model, messages, json_schema)
            cached = cache.get(cache_key_val)
            if cached is not None:
                cache_hit = True
                cache.stats.record_hit()
                latency_ms = int((time.monotonic() - start) * 1000)
                try:
                    self._resolve_ledger().record(
                        LedgerEvent(
                            request_id=request_id,
                            session_id=session_id,
                            user_id=user_id,
                            call_site=call_site,
                            usage=TokenUsage(
                                prompt_tokens=0,
                                completion_tokens=0,
                                total_tokens=0,
                                model=model,
                                cost_usd=0.0,
                            ),
                            latency_ms=latency_ms,
                            degraded=cached.degraded,
                            occurred_at_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        )
                    )
                except Exception:
                    logger.exception("llm: ledger.record failed for cache hit")
                return cached

        try:
            if not snowflake_available():
                degraded = True
                error = "snowflake unavailable"
                return ChatResult(
                    content="",
                    usage=TokenUsage(0, 0, 0, model, 0.0),
                    degraded=True,
                    error=error,
                )

            options: dict = {"max_tokens": max_output_tokens}
            if json_schema is not None:
                options["response_format"] = {"type": "json", "schema": json_schema}

            # Accept either backend.contracts.models.Message instances or
            # plain {"role", "content"} dicts - most call sites across the
            # pipeline still build prompts as dicts, matching the wire shape
            # Cortex COMPLETE itself expects, rather than constructing
            # Message objects.
            def _role(m):
                return m.role if hasattr(m, "role") else m["role"]

            def _content(m):
                return m.content if hasattr(m, "content") else m["content"]

            payload = [{"role": _role(m), "content": _content(m)} for m in messages]
            prompt_text_all = "\n".join(_content(m) for m in messages)

            raw_response = None
            last_exc: Exception | None = None
            for attempt in range(_MAX_RETRIES):
                try:
                    raw_response = self._call_complete(payload, model, options)
                    last_exc = None
                    break
                except Exception as exc:  # transient Snowflake errors only
                    last_exc = exc
                    logger.warning("llm: COMPLETE attempt %d failed: %s", attempt, exc)
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(2**attempt)

            if last_exc is not None:
                degraded = True
                error = str(last_exc)
                return ChatResult(
                    content="",
                    usage=TokenUsage(0, 0, 0, model, 0.0),
                    degraded=True,
                    error=error,
                )

            parsed_meta = try_parse_json(raw_response) if raw_response else None
            if isinstance(parsed_meta, dict) and "choices" in parsed_meta:
                # Cortex COMPLETE's structured response shape.
                choice = parsed_meta["choices"][0]
                content = choice.get("messages") or choice.get("content") or ""
                usage_meta = parsed_meta.get("usage")
                if usage_meta:
                    prompt_tokens = int(usage_meta.get("prompt_tokens", 0))
                    completion_tokens = int(usage_meta.get("completion_tokens", 0))
                else:
                    estimated = True
                    prompt_tokens = estimate_tokens(prompt_text_all)
                    completion_tokens = estimate_tokens(content)
            else:
                # Plain string response.
                content = raw_response or ""
                estimated = True
                prompt_tokens = estimate_tokens(prompt_text_all)
                completion_tokens = estimate_tokens(content)

            if json_schema is not None and try_parse_json(content) is None:
                # one repair attempt
                repair_msgs = build_repair_messages(payload, content, json_schema)
                try:
                    raw_repair = self._call_complete(repair_msgs, model, options)
                    repair_meta = try_parse_json(raw_repair) if raw_repair else None
                    if isinstance(repair_meta, dict) and "choices" in repair_meta:
                        repair_content = repair_meta["choices"][0].get("messages") or repair_meta["choices"][0].get("content") or ""
                    else:
                        repair_content = raw_repair or ""
                    if try_parse_json(repair_content) is not None:
                        content = repair_content
                        if estimated:
                            completion_tokens = estimate_tokens(content)
                    else:
                        degraded = True
                        error = "response did not parse as JSON after one repair attempt"
                except Exception as exc:
                    degraded = True
                    error = f"repair attempt failed: {exc}"

            pricing = self._load_pricing()
            cost_usd = compute_cost_usd(prompt_tokens, completion_tokens, model, pricing)
            if estimated:
                logger.info("llm: token counts estimated=True for call_site=%s", call_site)

            result = ChatResult(
                content=content,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    model=model,
                    cost_usd=cost_usd,
                ),
                degraded=degraded,
                error=error,
            )
            if cache_key_val is not None:
                cache.stats.record_miss(cost_usd=cost_usd)
                if not degraded:
                    cache.set(cache_key_val, result)
            return result
        except Exception as exc:  # belt-and-braces: never raise out of chat()
            logger.exception("llm: unhandled error in chat()")
            degraded = True
            error = str(exc)
            return ChatResult(
                content="",
                usage=TokenUsage(0, 0, 0, model, 0.0),
                degraded=True,
                error=error,
            )
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            try:
                cost_for_ledger = 0.0 if degraded else compute_cost_usd(
                    prompt_tokens, completion_tokens, model, self._pricing_cache or {}
                )
                self._resolve_ledger().record(
                    LedgerEvent(
                        request_id=request_id,
                        session_id=session_id,
                        user_id=user_id,
                        call_site=call_site,
                        usage=TokenUsage(
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=prompt_tokens + completion_tokens,
                            model=model,
                            cost_usd=cost_for_ledger,
                        ),
                        latency_ms=latency_ms,
                        degraded=degraded,
                        occurred_at_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                )
            except Exception:
                logger.exception("llm: ledger.record failed in finally block")

    def health(self) -> dict:
        if not snowflake_available():
            return {"ok": False, "detail": "snowflake unavailable"}
        try:
            pricing = self._load_pricing()
            model = _active_model()
            if model not in pricing:
                return {"ok": False, "detail": f"model {model!r} missing from MODEL_PRICING"}
            return {"ok": True, "detail": f"cortex complete ready, model={model}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
