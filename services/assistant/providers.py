"""LLM providers behind one small interface. Keys stay on the server."""

import asyncio
import contextvars
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.config import settings

TIMEOUT = httpx.Timeout(45.0, connect=10.0)
PROBE_TIMEOUT = httpx.Timeout(12.0, connect=5.0)
request_timeout: contextvars.ContextVar[httpx.Timeout] = contextvars.ContextVar("request_timeout", default=TIMEOUT)

_EXHAUSTED = (
    "insufficient_quota",
    "quota",
    "credit",
    "billing",
    "payment required",
    "resource_exhausted",
    "tokens per day",
    "tpd",
    "exceeded your current",
)


class ProviderError(RuntimeError):
    def __init__(self, message: str, kind: str = "down"):
        super().__init__(message)
        self.kind = kind


def failure_kind(status_code: int, body: str) -> str:
    """Classify a provider rejection. Credits and dead keys hide Striker; a short rate limit does not."""
    text = (body or "").lower()
    if status_code in (401, 403, 404) or "model_not_found" in text or "does not exist" in text:
        return "unavailable"
    if status_code == 402 or any(marker in text for marker in _EXHAUSTED):
        return "exhausted"
    if status_code == 429:
        return "limited"
    return "down"


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict


class Session:
    async def step(self, *, allow_tools: bool = True) -> Reply:
        raise NotImplementedError

    def add_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        raise NotImplementedError


class Provider:
    name = ""
    label = ""

    def session(self, system: str, history: list[dict], tools: list[ToolSpec]) -> Session:
        raise NotImplementedError


def _gemini_schema(node: dict) -> dict:
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "type":
            result[key] = str(value).upper()
        elif key == "properties":
            result[key] = {name: _gemini_schema(child) for name, child in value.items()}
        elif key == "items":
            result[key] = _gemini_schema(value)
        else:
            result[key] = value
    return result


async def _exchange(client: httpx.AsyncClient, provider: str, method: str, url: str, **kwargs) -> dict:
    try:
        response = await client.request(method, url, timeout=request_timeout.get(), **kwargs)
    except httpx.HTTPError as error:
        raise ProviderError(f"{provider} is unreachable: {type(error).__name__}") from None
    if response.status_code >= 400:
        detail = response.text[:300]
        raise ProviderError(
            f"{provider} returned {response.status_code}: {detail}", failure_kind(response.status_code, detail)
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise ProviderError(f"{provider} returned a non-JSON response") from None


async def _post(client: httpx.AsyncClient, provider: str, url: str, **kwargs) -> dict:
    return await _exchange(client, provider, "POST", url, **kwargs)


class GeminiSession(Session):
    def __init__(self, provider: "GeminiProvider", system: str, history: list[dict], tools: list[ToolSpec]):
        self.provider = provider
        self.system = system
        self.contents: list[dict] = [
            {"role": "model" if item["role"] == "assistant" else "user", "parts": [{"text": item["content"]}]}
            for item in history
        ]
        self.declarations = []
        for tool in tools:
            declaration: dict[str, Any] = {"name": tool.name, "description": tool.description}
            if tool.parameters.get("properties"):
                declaration["parameters"] = _gemini_schema(tool.parameters)
            self.declarations.append(declaration)

    async def step(self, *, allow_tools: bool = True) -> Reply:
        generation: dict[str, Any] = {"temperature": 0.2, "maxOutputTokens": 2048}
        if "2.5-flash" in self.provider.model:
            generation["thinkingConfig"] = {"thinkingBudget": 0}
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": self.contents,
            "generationConfig": generation,
        }
        if self.declarations:
            body["tools"] = [{"functionDeclarations": self.declarations}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO" if allow_tools else "NONE"}}
        url = f"{self.provider.base_url}/models/{self.provider.model}:generateContent"
        data = await _post(
            self.provider.client, "Gemini", url, json=body, headers={"x-goog-api-key": self.provider.api_key}
        )
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            if reason:
                return Reply(text="I can't help with that request.")
            raise ProviderError("Gemini returned no candidates")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        if not parts:
            if candidates[0].get("finishReason") in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}:
                return Reply(text="I can't help with that request.")
            raise ProviderError(f"Gemini returned an empty answer ({candidates[0].get('finishReason')})")
        self.contents.append({"role": "model", "parts": parts})
        reply = Reply()
        texts = []
        for part in parts:
            if part.get("thought"):
                continue
            if "text" in part:
                texts.append(part["text"])
            call = part.get("functionCall")
            if call:
                reply.calls.append(
                    ToolCall(id=call.get("id") or "", name=call.get("name", ""), args=call.get("args") or {})
                )
        reply.text = "".join(texts).strip()
        return reply

    def add_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        parts = []
        for call, result in results:
            response: dict[str, Any] = {"name": call.name, "response": {"result": result}}
            if call.id:
                response["id"] = call.id
            parts.append({"functionResponse": response})
        self.contents.append({"role": "user", "parts": parts})


class GeminiProvider(Provider):
    name = "gemini"
    label = "Google Gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient):
        self.api_key = api_key
        self.model = model
        self.client = client

    def session(self, system, history, tools) -> Session:
        return GeminiSession(self, system, history, tools)


class OpenAISession(Session):
    def __init__(self, provider: "OpenAICompatibleProvider", system: str, history: list[dict], tools: list[ToolSpec]):
        self.provider = provider
        self.messages: list[dict] = [{"role": "system", "content": system}] + [
            {"role": item["role"], "content": item["content"]} for item in history
        ]
        self.tools = [
            {
                "type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
            }
            for tool in tools
        ]

    async def step(self, *, allow_tools: bool = True) -> Reply:
        body: dict[str, Any] = {
            "model": self.provider.model,
            "messages": self.messages,
            "temperature": 0.2,
        }
        body.update(self.provider.request_body)
        if "max_tokens" not in body and "max_completion_tokens" not in body:
            body["max_tokens"] = 1500
        if self.tools and allow_tools:
            body["tools"] = self.tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.provider.api_key}", **self.provider.extra_headers}
        data = await _post(self.provider.client, self.provider.label, self.provider.url, json=body, headers=headers)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.provider.label} returned no choices")
        message = choices[0].get("message") or {}
        reply = Reply(text=(message.get("content") or "").strip())
        tool_calls = message.get("tool_calls") or []
        for item in tool_calls:
            function = item.get("function") or {}
            raw = function.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                args = {}
            reply.calls.append(ToolCall(id=item.get("id") or uuid.uuid4().hex, name=function.get("name", ""), args=args or {}))
        entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.args)},
                }
                for call in reply.calls
            ]
        self.messages.append(entry)
        return reply

    def add_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        for call, result in results:
            self.messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)}
            )


class OpenAICompatibleProvider(Provider):
    def __init__(
        self,
        name: str,
        label: str,
        url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient,
        extra_headers=None,
        request_body=None,
    ):
        self.name = name
        self.label = label
        self.url = url
        self.api_key = api_key
        self.model = model
        self.client = client
        self.extra_headers = extra_headers or {}
        self.request_body = request_body or {}

    def session(self, system, history, tools) -> Session:
        return OpenAISession(self, system, history, tools)


CURSOR_API = "https://api.cursor.com"
CURSOR_WAIT_SECONDS = 50


def cursor_reply(text: str) -> Reply:
    """A Cursor run returns either the user's answer or one JSON tool-call object."""
    raw = (text or "").strip()
    fenced = raw
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fenced = "\n".join(lines).strip()
    try:
        data = json.loads(fenced)
    except ValueError:
        return Reply(text=raw)
    if not isinstance(data, dict):
        return Reply(text=raw)
    if "tool_calls" in data:
        calls = []
        for item in data.get("tool_calls") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            arguments = item.get("arguments")
            calls.append(
                ToolCall(
                    id=uuid.uuid4().hex,
                    name=str(item["name"]),
                    args=arguments if isinstance(arguments, dict) else {},
                )
            )
        if calls:
            return Reply(calls=calls)
        return Reply(text=raw)
    for key in ("reply", "text", "answer"):
        answer = data.get(key)
        if isinstance(answer, str) and answer.strip():
            return Reply(text=answer.strip())
    return Reply(text=raw)


class CursorSession(Session):
    def __init__(self, provider: "CursorProvider", system: str, history: list[dict], tools: list[ToolSpec]):
        self.provider = provider
        self.system = system
        self.turns = [{"role": item["role"], "content": item["content"]} for item in history]
        self.tools = tools

    def prompt(self, *, allow_tools: bool) -> str:
        lines = [self.system, "", "Answer the conversation. Do not edit files, open a repository, or write code."]
        if self.tools and allow_tools:
            lines.append("To use a tool, reply with only this JSON and no markdown:")
            lines.append('{"tool_calls":[{"name":"tool_name","arguments":{}}]}')
            lines.append("Tools:")
            for tool in self.tools:
                lines.append(f"- {tool.name}: {tool.description}")
                lines.append(json.dumps(tool.parameters))
        else:
            lines.append("Reply to the user in markdown. Do not call a tool.")
        lines.append("")
        lines.append("Conversation:")
        for turn in self.turns:
            lines.append(f"{turn['role']}: {turn['content']}")
        return "\n".join(lines)

    async def step(self, *, allow_tools: bool = True) -> Reply:
        reply = cursor_reply(await self.provider.complete(self.prompt(allow_tools=allow_tools)))
        if reply.calls:
            payload = {"tool_calls": [{"name": call.name, "arguments": call.args} for call in reply.calls]}
            self.turns.append({"role": "assistant", "content": json.dumps(payload)})
        else:
            self.turns.append({"role": "assistant", "content": reply.text})
        return reply

    def add_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        for call, result in results:
            self.turns.append(
                {
                    "role": "tool",
                    "content": json.dumps({"name": call.name, "result": result}, default=str),
                }
            )


class CursorProvider(Provider):
    """Cursor Cloud Agents API. Each step is one short run with no repository."""

    name = "cursor"
    label = "Cursor"

    def __init__(self, api_key: str, model: str | None, client: httpx.AsyncClient):
        self.api_key = api_key
        self.model = (model or "").strip() or None
        self.client = client

    def session(self, system, history, tools) -> Session:
        return CursorSession(self, system, history, tools)

    async def check(self) -> None:
        await self._json("GET", "/v1/me")

    async def complete(self, prompt: str) -> str:
        body: dict[str, Any] = {"prompt": {"text": prompt}, "mode": "plan", "autoCreatePR": False}
        if self.model:
            body["model"] = {"id": self.model}
        created = await self._json("POST", "/v1/agents", json=body)
        agent = created.get("agent") or {}
        run = created.get("run") or {}
        agent_id = agent.get("id")
        run_id = run.get("id") or agent.get("latestRunId")
        if not agent_id or not run_id:
            raise ProviderError("Cursor returned no agent run")
        try:
            return await self._wait(agent_id, run_id)
        finally:
            await self._quietly("POST", f"/v1/agents/{agent_id}/archive")

    async def _wait(self, agent_id: str, run_id: str) -> str:
        deadline = time.monotonic() + CURSOR_WAIT_SECONDS
        while True:
            run = await self._json("GET", f"/v1/agents/{agent_id}/runs/{run_id}")
            status = run.get("status")
            if status == "FINISHED":
                result = (run.get("result") or "").strip()
                if not result:
                    raise ProviderError("Cursor returned an empty answer")
                return result
            if status in {"ERROR", "CANCELLED", "EXPIRED"}:
                raise ProviderError(f"Cursor run {status}")
            if time.monotonic() >= deadline:
                await self._quietly("POST", f"/v1/agents/{agent_id}/runs/{run_id}/cancel")
                raise ProviderError("Cursor did not answer in time")
            await asyncio.sleep(1.5)

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        return await _exchange(
            self.client,
            "Cursor",
            method,
            f"{CURSOR_API}{path}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            **kwargs,
        )

    async def _quietly(self, method: str, path: str) -> None:
        try:
            await self._json(method, path)
        except ProviderError:
            return


def configured_providers(client: httpx.AsyncClient) -> list[Provider]:
    """Providers in fallback order. Only those with a key are returned."""
    providers: list[Provider] = []
    if settings.GEMINI_API_KEY:
        providers.append(GeminiProvider(settings.GEMINI_API_KEY, settings.GEMINI_MODEL, client))
    if settings.GROQ_API_KEY:
        providers.append(
            OpenAICompatibleProvider(
                "groq",
                "Groq",
                "https://api.groq.com/openai/v1/chat/completions",
                settings.GROQ_API_KEY,
                settings.GROQ_MODEL,
                client,
                request_body={
                    "max_completion_tokens": 4096,
                    "reasoning_effort": "low",
                    "reasoning_format": "hidden",
                },
            )
        )
    if settings.OPENROUTER_API_KEY:
        providers.append(
            OpenAICompatibleProvider(
                "openrouter",
                "OpenRouter",
                "https://openrouter.ai/api/v1/chat/completions",
                settings.OPENROUTER_API_KEY,
                settings.OPENROUTER_MODEL,
                client,
                extra_headers={"X-Title": "CPL Striker"},
            )
        )
    if settings.CURSOR_API_KEY:
        providers.append(CursorProvider(settings.CURSOR_API_KEY, settings.CURSOR_MODEL, client))
    return providers
