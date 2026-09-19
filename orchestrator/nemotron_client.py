"""
Thin wrapper around NVIDIA's OpenAI-compatible Nemotron endpoint.

Why this file exists instead of calling the OpenAI SDK directly:
  1. Nemotron is a *reasoning* model. Reasoning tokens and answer tokens share
     one max_tokens budget. If reasoning eats the budget you get
     finish_reason="length" and an EMPTY answer. This wrapper detects that and
     raises a loud, actionable error instead of returning "".
  2. The reasoning trace lands in different fields depending on the deployment
     (`reasoning`, `reasoning_content`, or inline <think>...</think>). This
     normalizes all three.
  3. Free-tier build.nvidia.com is ~40 requests/min. This retries 429s.
  4. MOCK=1 short-circuits every call so you can develop the loop without
     burning credits.
"""

from __future__ import annotations

import config  # noqa: F401  - loads .env before anything reads it
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
MODE = os.getenv("MODE", "mock").lower()   # mock | live | replay

# Model IDs as they appear on build.nvidia.com. Verify with:
#   curl -s https://integrate.api.nvidia.com/v1/models \
#     -H "Authorization: Bearer $NVIDIA_API_KEY" | python3 -m json.tool
ORCHESTRATOR_MODEL = os.getenv("NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b")
FAST_MODEL = os.getenv("NEMOTRON_FAST_MODEL", "nvidia/nemotron-3-nano-30b-a3b")

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_ORPHAN_CLOSE = re.compile(r"^.*?</think>\s*", re.DOTALL)


class NemotronBudgetError(RuntimeError):
    """Reasoning consumed the whole token budget and no answer came back."""


class NemotronError(RuntimeError):
    pass


@dataclass
class Reply:
    """Normalized Nemotron response."""

    content: str = ""  # empty when the model only emitted tool calls
    reasoning: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    raw_message: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def json(self) -> Any:
        """Parse content as JSON, tolerating markdown fences."""
        text = self.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return json.loads(text)


class NemotronClient:
    """
    Usage:
        nem = NemotronClient()
        reply = nem.chat([{"role": "user", "content": "hi"}], thinking="off")
        print(reply.content)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_retries: int = 4,
        mock: bool | None = None,
    ):
        self.model = model or ORCHESTRATOR_MODEL
        self.base_url = base_url or BASE_URL
        self.max_retries = max_retries
        self.mock = (MODE == "mock") if mock is None else mock
        self.call_log: list[dict] = []  # for the demo: every request/response pair

        self._client = None
        if not self.mock:
            api_key = api_key or os.environ.get("NVIDIA_API_KEY")
            if not api_key:
                raise NemotronError(
                    "NVIDIA_API_KEY is not set. Get one at "
                    "https://build.nvidia.com/settings/api-keys (starts with 'nvapi-'), "
                    "or run with MODE=mock to develop without the API."
                )
            # Imported lazily so MOCK=1 and unit tests work without the SDK.
            from openai import OpenAI

            self._client = OpenAI(base_url=self.base_url, api_key=api_key)

    # ----------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        thinking: str = "off",  # "off" | "low" | "on"
        max_tokens: int = 1024,
        thinking_token_budget: int | None = None,
        temperature: float | None = None,
        top_p: float = 0.95,
        json_mode: bool = False,
        model: str | None = None,
    ) -> Reply:
        """
        thinking:
          "off"  -> direct answer. Whole max_tokens budget goes to the answer.
                    Use for routing, JSON extraction, and the Alexa path.
          "low"  -> brief reasoning. Good default for tool selection.
          "on"   -> full reasoning trace in reply.reasoning. Use for planning,
                    and for the demo (a visible trace is your "beyond the
                    chatbot" evidence).

        Note: json_mode with thinking != "off" is a trap — reasoning can eat the
        budget before the JSON is emitted. This enforces thinking="off" for
        json_mode unless you explicitly pass a thinking_token_budget.
        """
        if json_mode and thinking != "off" and thinking_token_budget is None:
            thinking = "off"

        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        # Reasoning control rides in extra_body as chat_template_kwargs.
        ctk: dict[str, Any] = {"enable_thinking": thinking != "off"}
        if thinking == "low":
            ctk["low_effort"] = True
        if thinking == "off":
            # Guards against an empty `content` when reasoning is suppressed.
            ctk["force_nonempty_content"] = True
        extra: dict[str, Any] = {"chat_template_kwargs": ctk}
        if thinking != "off" and thinking_token_budget:
            extra["thinking_token_budget"] = thinking_token_budget
        kwargs["extra_body"] = extra

        if self.mock:
            return self._mock_reply(kwargs)

        # Everything goes through cache.py so MODE=replay can serve this from
        # disk for free. In live mode the response is recorded on the way past.
        import cache

        cache_request = {"provider": "nvidia", **{
            k: v for k, v in kwargs.items() if k != "extra_body"
        }, "ctk": ctk}

        def do_call() -> dict:
            resp = self._with_retries(
                lambda: self._client.chat.completions.create(**kwargs)
            )
            return _as_dict(_normalize(resp))

        reply = _from_dict(cache.wrap(
            label=f"nemotron:{len(messages)}msg:{thinking}",
            request=cache_request,
            call=do_call,
            mock_value=None,  # unreachable: mock mode returns earlier
        ))
        self.call_log.append(
            {
                "model": kwargs["model"],
                "thinking": thinking,
                "n_tools_offered": len(tools or []),
                "tool_calls": [tc["function"]["name"] for tc in reply.tool_calls],
                "finish_reason": reply.finish_reason,
                "usage": reply.usage,
            }
        )

        if reply.finish_reason == "length" and not reply.content and not reply.tool_calls:
            raise NemotronBudgetError(
                f"Nemotron hit max_tokens={max_tokens} during reasoning and returned "
                f"no answer. Fix by one of: raise max_tokens, set "
                f"thinking_token_budget (must be < max_tokens), or pass "
                f"thinking='off'. Reasoning tokens used: "
                f"{reply.usage.get('reasoning_tokens', '?')}"
            )
        return reply

    # ----------------------------------------------------------------------

    def _with_retries(self, fn: Callable[[], Any]) -> Any:
        last = None
        for attempt in range(self.max_retries):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - SDK exception types vary
                last = exc
                status = getattr(exc, "status_code", None)
                retryable = status in (408, 409, 429, 500, 502, 503, 504)
                if not retryable or attempt == self.max_retries - 1:
                    raise
                # Free tier is ~40 req/min, so a 429 means genuinely wait.
                sleep = (2.0**attempt) + random.uniform(0, 1.0)
                if status == 429:
                    sleep = max(sleep, 3.0)
                print(f"[nemotron] {status}, retrying in {sleep:.1f}s "
                      f"({attempt + 1}/{self.max_retries})")
                time.sleep(sleep)
        raise last  # type: ignore[misc]

    def _mock_reply(self, kwargs: dict) -> Reply:
        """
        A fake Nemotron that emits plausible tool-call sequences.

        This exists so the whole backend runs end to end with no API key and no
        credits: Kenneth and Rowan can develop against a live server that
        behaves realistically. It is NOT smart -- it matches keywords. But it
        exercises every code path the real model would.

        It decides which turn it's on by looking at the history: if the last
        message is a tool result, it's time to write a summary; otherwise it's
        time to call tools.
        """
        self.call_log.append({"MOCK": True, "model": kwargs["model"]})
        messages = kwargs.get("messages") or []

        if kwargs.get("response_format"):
            return Reply(content='{"mock": true}')

        # Already have tool results -> write the summary and stop.
        if messages and messages[-1].get("role") == "tool":
            names = [
                tc["function"]["name"]
                for m in messages if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])
            ]
            return Reply(
                content=(
                    "[MOCK] Based on what's stored, you have four open items this "
                    "week. The CS project proposal is the largest at about four "
                    "hours, and the physics quiz is soonest. "
                    f"(mock ran: {', '.join(names) or 'nothing'})"
                )
            )

        prompt = " ".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "user"
        ).lower()
        available = {t["function"]["name"] for t in (kwargs.get("tools") or [])}
        plan = _mock_plan(prompt, available)

        if not plan:
            return Reply(content="[MOCK] Nothing to look up for that.")

        return Reply(
            reasoning=(
                f"[MOCK reasoning] The student's request looks like it needs: "
                f"{', '.join(n for n, _ in plan)}. Reading stored data before "
                f"anything expensive."
            ),
            tool_calls=[
                {"id": f"mock-{i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(plan)
            ],
        )


def _mock_plan(prompt: str, available: set[str]) -> list[tuple[str, dict]]:
    """Keyword -> tool sequence. Only returns tools that were actually offered."""
    def has(*words: str) -> bool:
        return any(w in prompt for w in words)

    if has("refresh", "sync", "re-read", "latest", "up to date", "update canvas"):
        plan = [("check_freshness", {}), ("refresh_from_canvas", {}),
                ("get_assignments", {"due_within_days": 7})]
    elif has("schedule", "plan my", "when should i", "time block"):
        plan = [("get_assignments", {"due_within_days": 14}),
                ("make_schedule", {"horizon_days": 7})]
    elif has("study", "review", "flashcard", "practice", "prepare for"):
        plan = [("make_study_guide", {"course": "PHYS 1361",
                                      "topics": ["Gauss's law", "electric potential"]})]
    elif has("event", "career fair", "on campus", "happening"):
        plan = [("get_events", {"within_days": 14})]
    elif has("trend", "workload", "busier", "last week", "over time", "history"):
        plan = [("get_workload_history", {"days": 30})]
    elif has("remember", "i prefer", "my name is", "call me", "don't schedule"):
        plan = [("update_preferences", {"updates": {"note": "captured from prompt"}})]
    elif has("spent", "worked on", "log"):
        plan = [("log_time", {"minutes": 90})]
    else:
        plan = [("check_freshness", {}), ("get_assignments", {"due_within_days": 7})]

    return [(n, a) for n, a in plan if n in available]


# --------------------------------------------------------------------------
# Response normalization
# --------------------------------------------------------------------------


def _as_dict(reply: Reply) -> dict:
    """Reply -> plain dict, so it can be written to the cache as JSON."""
    return {
        "content": reply.content,
        "reasoning": reply.reasoning,
        "tool_calls": reply.tool_calls,
        "finish_reason": reply.finish_reason,
        "usage": reply.usage,
    }


def _from_dict(data: dict) -> Reply:
    """Plain dict -> Reply, for reading back out of the cache."""
    return Reply(
        content=data.get("content", ""),
        reasoning=data.get("reasoning"),
        tool_calls=data.get("tool_calls") or [],
        finish_reason=data.get("finish_reason", "stop"),
        usage=data.get("usage") or {},
    )


def _normalize(resp: Any) -> Reply:
    choice = resp.choices[0]
    msg = choice.message

    content = msg.content or ""

    # The reasoning trace shows up in one of three places depending on whether
    # the server has a reasoning parser configured.
    reasoning = (
        getattr(msg, "reasoning_content", None)
        or getattr(msg, "reasoning", None)
        or None
    )
    if reasoning is None and "</think>" in content:
        match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
        reasoning = match.group(1).strip() if match else None
        content = _THINK_BLOCK.sub("", content)
        if "</think>" in content:  # opening tag was a special token, not text
            content = _ORPHAN_CLOSE.sub("", content)

    tool_calls: list[dict] = []
    for tc in getattr(msg, "tool_calls", None) or []:
        tool_calls.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
        )

    usage = {}
    if getattr(resp, "usage", None):
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        }
        details = getattr(resp.usage, "completion_tokens_details", None)
        if details:
            usage["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)

    return Reply(
        content=content.strip(),
        reasoning=reasoning,
        tool_calls=tool_calls,
        finish_reason=choice.finish_reason or "stop",
        usage=usage,
        raw_message=msg,
    )
