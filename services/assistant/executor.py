"""Runs Striker tools by calling the API in-process, so every route keeps its own validation and permissions."""

import json
from typing import Any

import httpx
from pydantic import ValidationError

from services.assistant.tools import Tool

RESULT_LIMIT = 12_000
_DROP_KEYS = {"thumbnail_url", "placeholder_url", "storage_key", "avatar_storage_key", "token_version"}


class ToolArgumentError(ValueError):
    pass


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _DROP_KEYS and item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")))


def _largest_list(value: Any) -> list | None:
    best: list | None = None

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, list):
            if len(node) > 1 and (best is None or _size(node) > _size(best)):
                best = node
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(value)
    return best


def shrink(value: Any, limit: int = RESULT_LIMIT) -> tuple[Any, bool]:
    """Halve the largest lists until the JSON fits; the model is told the result was cut."""
    truncated = False
    for _ in range(40):
        if _size(value) <= limit:
            return value, truncated
        target = _largest_list(value)
        if target is None:
            break
        del target[max(1, len(target) // 2):]
        truncated = True
    text = json.dumps(value, default=str)
    if len(text) > limit:
        return text[:limit], True
    return value, truncated


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and value > 0 else None


def split_arguments(tool: Tool, args: dict) -> tuple[str, dict, dict | None, dict | None]:
    """Map flat model arguments onto path, query and body. Raises ToolArgumentError on bad input."""
    if not isinstance(args, dict):
        raise ToolArgumentError("Arguments must be an object.")
    path = tool.path
    for name in tool.path_params:
        number = positive_int(args.get(name))
        if number is None:
            raise ToolArgumentError(f"{name} is required and must be a positive whole number.")
        path = path.replace("{" + name + "}", str(number))

    query = {}
    for arg, param in tool.query.items():
        if args.get(arg) not in (None, ""):
            query[param] = args[arg]
    for name in tool.extra.get("required", []) if tool.extra else []:
        if args.get(name) in (None, "") and name not in tool.form:
            raise ToolArgumentError(f"{name} is required.")

    body = None
    form = None
    if tool.body is not None:
        body = {key: value for key, value in args.items() if key in tool.body_fields}
        try:
            tool.body.model_validate(body)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in item['loc']) or 'body'}: {item['msg']}" for item in error.errors()
            )
            raise ToolArgumentError(problems) from None
    elif tool.form:
        form = {key: str(args[key]) for key in tool.form if args.get(key) not in (None, "")}
        missing = [name for name in (tool.extra or {}).get("required", []) if name not in form]
        if missing:
            raise ToolArgumentError(f"{', '.join(missing)} is required.")
    return path, query, body, form


class ToolExecutor:
    def __init__(self, app, *, authorization: str | None = None, public: bool = False, client: httpx.AsyncClient | None = None):
        self.public = public
        headers = {"Authorization": authorization} if authorization else {}
        self.client = client or httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://striker.internal",
            headers=headers,
            timeout=30.0,
        )
        self._visible: set[int] | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def _call(self, method: str, path: str, query=None, body=None, form=None) -> tuple[int, dict]:
        kwargs: dict[str, Any] = {"params": query or None}
        if body is not None:
            kwargs["json"] = body
        if form is not None:
            kwargs["data"] = form
        response = await self.client.request(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"success": response.is_success, "message": response.text[:300], "data": None}
        if not isinstance(payload, dict):
            payload = {"success": response.is_success, "message": None, "data": payload}
        return response.status_code, payload

    async def visible_seasons(self) -> set[int]:
        if self._visible is None:
            status, payload = await self._call("GET", "/seasons/")
            seasons = payload.get("data") or [] if status == 200 else []
            self._visible = {season["id"] for season in seasons if season.get("show", True)}
        return self._visible

    async def _guard(self, tool: Tool, args: dict) -> str | None:
        if not self.public:
            return None
        visible = await self.visible_seasons()
        if tool.season_guard and positive_int(args.get(tool.season_guard)) not in visible:
            return "That season is not available."
        if tool.match_guard:
            status, payload = await self._call("GET", f"/matches/{positive_int(args.get('match_id'))}")
            match = payload.get("data") if status == 200 else None
            if not match or match.get("season_id") not in visible:
                return "That match is not available."
        return None

    def _prune(self, data: Any, visible: set[int]) -> Any:
        if isinstance(data, list):
            return [
                self._prune(item, visible)
                for item in data
                if not (isinstance(item, dict) and "season_id" in item and item["season_id"] not in visible)
            ]
        if isinstance(data, dict):
            return {key: self._prune(value, visible) for key, value in data.items()}
        return data

    async def run(self, tool: Tool, args: dict) -> dict:
        """Return a compact result for the model: ok, message, and data or error."""
        try:
            path, query, body, form = split_arguments(tool, args)
            blocked = await self._guard(tool, args)
        except ToolArgumentError as error:
            return {"ok": False, "error": str(error)}
        if blocked:
            return {"ok": False, "error": blocked}

        status, payload = await self._call(tool.method, path, query, body, form)
        ok = 200 <= status < 300 and payload.get("success", True) is not False
        message = payload.get("message")
        if not ok and not message and isinstance(payload.get("detail"), str):
            message = payload["detail"]
        if not ok:
            detail = payload.get("data") if status == 422 else None
            error = message or f"Request failed with status {status}."
            if status >= 500:
                error = "The server could not complete that request."
            return {"ok": False, "status": status, "error": error, **({"details": _clean(detail)} if detail else {})}

        data = _clean(payload.get("data"))
        if self.public:
            visible = await self.visible_seasons()
            if tool.name == "list_seasons" and isinstance(data, list):
                data = [season for season in data if season.get("id") in visible]
            if tool.prune_hidden:
                data = self._prune(data, visible)
        data, truncated = shrink(data)
        result = {"ok": True, "message": message, "data": data}
        if truncated:
            result["note"] = "Result was shortened to fit. Ask for something narrower if needed."
        return result
