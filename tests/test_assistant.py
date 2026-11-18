import asyncio
import json
import unittest
from unittest import mock

from tests import test_auth_service as _env  # noqa: F401  (sets the environment before app imports)

from fastapi import FastAPI, HTTPException

from core.response import ApiResponse
from schemas.auth_schema import AdminResponse
from services.assistant import actions
from services.assistant.engine import Engine
from services.assistant.executor import ToolArgumentError, ToolExecutor, shrink, split_arguments
from services.assistant.providers import Provider, ProviderError, Reply, Session, ToolCall
from services.assistant.schema import model_parameters
from services.assistant.tools import CATALOG, tools_for

_ALLOWED_KEYS = {"type", "description", "enum", "items", "properties", "required"}


def run(coro):
    return asyncio.run(coro)


def _walk(node, path="root"):
    yield path, node
    for name, child in node.get("properties", {}).items():
        yield from _walk(child, f"{path}.{name}")
    if "items" in node:
        yield from _walk(node["items"], f"{path}[]")


class SchemaTests(unittest.TestCase):
    def test_every_tool_schema_uses_the_shared_subset(self):
        for tool in CATALOG.values():
            parameters = tool.parameters()
            self.assertEqual(parameters["type"], "object", tool.name)
            for path, node in _walk(parameters):
                self.assertLessEqual(set(node), _ALLOWED_KEYS, f"{tool.name} {path}")
                self.assertIn(node["type"], {"string", "integer", "number", "boolean", "array", "object"})
                if "enum" in node:
                    self.assertTrue(all(isinstance(value, str) for value in node["enum"]))
            for name in parameters.get("required", []):
                self.assertIn(name, parameters["properties"], tool.name)

    def test_integer_enums_are_described_by_name(self):
        from schemas.match_schema import MatchCreateRequest

        category = model_parameters(MatchCreateRequest)["properties"]["category"]
        self.assertEqual(category["type"], "integer")
        self.assertIn("2 = Final", category["description"])

    def test_dates_and_nested_items(self):
        from schemas.match_schema import MatchStatsUpsertRequest

        stats = model_parameters(MatchStatsUpsertRequest)["properties"]["stats"]
        self.assertEqual(stats["items"]["required"], ["player_id"])
        season = CATALOG["create_season"].parameters()["properties"]["start_date"]
        self.assertIn("YYYY-MM-DD", season["description"])

    def test_names_are_unique_and_valid(self):
        for name in CATALOG:
            self.assertRegex(name, r"^[a-z][a-z0-9_]{2,63}$")


class CatalogTests(unittest.TestCase):
    def test_public_mode_is_read_only(self):
        public = tools_for("public")
        self.assertGreaterEqual(len(public), 15)
        self.assertTrue(all(tool.method == "GET" and not tool.write for tool in public))

    def test_every_tool_permission_is_known(self):
        from core.permissions import ALL_CODES

        for tool in CATALOG.values():
            self.assertLessEqual(set(tool.permissions), ALL_CODES, tool.name)
            if tool.write:
                self.assertNotEqual(tool.method, "GET", tool.name)

    def test_admin_tools_follow_permissions(self):
        names = {tool.name for tool in tools_for("admin", {"teams.create"})}
        self.assertIn("create_team", names)
        self.assertIn("list_teams", names)
        self.assertIn("update_admin", names)
        self.assertNotIn("delete_team", names)
        self.assertNotIn("delete_match", names)
        self.assertNotIn("list_admins", names)

    def test_full_admin_sees_every_tool(self):
        from core.permissions import ALL_CODES

        self.assertEqual(len(tools_for("admin", set(ALL_CODES))), len(CATALOG))

    def test_catalog_paths_match_real_routes(self):
        from fastapi.routing import APIRoute

        from main import app
        from routes.system_routes import _iter_routes

        routes = {
            (method, route.path.rstrip("/") or "/")
            for route in _iter_routes(app)
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        for tool in CATALOG.values():
            self.assertIn((tool.method, tool.path.rstrip("/") or "/"), routes, tool.name)


class ArgumentTests(unittest.TestCase):
    def test_path_query_and_body_are_split(self):
        path, query, body, form = split_arguments(
            CATALOG["update_season"], {"season_id": "4", "name": "Season Four", "show": False}
        )
        self.assertEqual(path, "/seasons/4")
        self.assertEqual(query, {})
        self.assertEqual(body, {"name": "Season Four", "show": False})
        self.assertIsNone(form)
        path, query, _, _ = split_arguments(CATALOG["get_team_roster"], {"team_id": 2, "season_id": 3})
        self.assertEqual((path, query), ("/rosters/team/2", {"seasonId": 3}))

    def test_bad_ids_are_rejected(self):
        for value in (None, 0, -1, "abc", True, "1;DROP"):
            with self.assertRaises(ToolArgumentError):
                split_arguments(CATALOG["delete_match"], {"match_id": value})

    def test_body_is_validated_before_confirmation(self):
        with self.assertRaises(ToolArgumentError) as raised:
            split_arguments(CATALOG["create_season"], {"name": "x"})
        self.assertIn("name", str(raised.exception))

    def test_form_tools_need_their_link(self):
        with self.assertRaises(ToolArgumentError):
            split_arguments(CATALOG["set_team_logo_link"], {"team_id": 1})
        _, _, body, form = split_arguments(CATALOG["set_team_logo_link"], {"team_id": 1, "url": "https://x.test/a.png"})
        self.assertIsNone(body)
        self.assertEqual(form, {"url": "https://x.test/a.png"})

    def test_large_results_are_shrunk(self):
        data = {"items": [{"id": index, "text": "x" * 200} for index in range(500)]}
        small, truncated = shrink(data, limit=5000)
        self.assertTrue(truncated)
        self.assertLessEqual(len(json.dumps(small)), 5000)
        self.assertGreater(len(small["items"]), 1)


def _stub_app():
    app = FastAPI()
    seasons = [
        {"id": 1, "name": "Season 1", "start_date": None, "end_date": None, "show": True},
        {"id": 2, "name": "Hidden", "start_date": None, "end_date": None, "show": False},
    ]
    matches = {10: {"id": 10, "season_id": 1}, 20: {"id": 20, "season_id": 2}}

    @app.get("/seasons/")
    def list_seasons():
        return ApiResponse(success=True, message="ok", data=seasons)

    @app.get("/seasons/{season_id}/league-table")
    def table(season_id: int):
        return ApiResponse(success=True, message="ok", data={"season_status": 2, "standings": []})

    @app.get("/matches/{match_id}")
    def match(match_id: int):
        return ApiResponse(success=True, message="ok", data=matches[match_id])

    @app.get("/rosters/player/{player_id}")
    def history(player_id: int):
        return ApiResponse(
            success=True,
            message="ok",
            data={"player_id": player_id, "seasons": [{"season_id": 1}, {"season_id": 2}]},
        )

    @app.delete("/matches/{match_id}")
    def delete(match_id: int):
        app.state.deleted.append(match_id)
        return ApiResponse(success=True, message="Match deleted successfully", data=True)

    @app.post("/teams/")
    def create_team():
        raise HTTPException(status_code=403, detail="You need Create teams.")

    app.state.deleted = []
    return app


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.app = _stub_app()

    async def _run(self, name, args, public=True):
        executor = ToolExecutor(self.app, public=public)
        try:
            return await executor.run(CATALOG[name], args)
        finally:
            await executor.close()

    def test_public_hides_hidden_seasons(self):
        result = run(self._run("list_seasons", {}))
        self.assertEqual([season["id"] for season in result["data"]], [1])
        blocked = run(self._run("get_league_table", {"season_id": 2}))
        self.assertFalse(blocked["ok"])
        allowed = run(self._run("get_league_table", {"season_id": 1}))
        self.assertTrue(allowed["ok"])

    def test_public_match_in_hidden_season_is_blocked(self):
        self.assertFalse(run(self._run("get_match", {"match_id": 20}))["ok"])
        self.assertTrue(run(self._run("get_match", {"match_id": 10}))["ok"])

    def test_history_rows_from_hidden_seasons_are_removed(self):
        result = run(self._run("get_player_history", {"player_id": 5}))
        self.assertEqual(result["data"]["seasons"], [{"season_id": 1}])

    def test_admin_mode_sees_hidden_seasons(self):
        result = run(self._run("list_seasons", {}, public=False))
        self.assertEqual(len(result["data"]), 2)

    def test_route_errors_come_back_as_messages(self):
        result = run(self._run("create_team", {"name": "Strikers"}, public=False))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)
        self.assertIn("Create teams", result["error"])


class ActionTests(unittest.TestCase):
    def test_round_trip(self):
        token = actions.sign("delete_match", {"match_id": 3}, 7)
        pending = actions.verify(token, 7)
        self.assertEqual((pending.tool, pending.args), ("delete_match", {"match_id": 3}))

    def test_other_admin_tamper_and_expiry_are_rejected(self):
        token = actions.sign("delete_match", {"match_id": 3}, 7)
        with self.assertRaises(actions.ActionError):
            actions.verify(token, 8)
        body, mac = token.split(".")
        forged = actions._b64(json.dumps({"t": "delete_match", "a": {"match_id": 4}, "u": 7, "e": 9999999999}).encode())
        with self.assertRaises(actions.ActionError):
            actions.verify(f"{forged}.{mac}", 7)
        with self.assertRaises(actions.ActionError):
            actions.verify(token, 7, now=10**12)
        with self.assertRaises(actions.ActionError):
            actions.verify("garbage", 7)

    def test_yes_and_no(self):
        for text in ("yes", "Yes!", "confirm", "go ahead", "do it."):
            self.assertTrue(actions.is_yes(text), text)
        for text in ("no", "cancel", "nevermind", "don't"):
            self.assertTrue(actions.is_no(text), text)
        self.assertFalse(actions.is_yes("yes but change the date"))

    def test_passwords_are_masked(self):
        self.assertEqual(actions.masked({"password": "secret-123456", "username": "a"})["password"], "••••••")


class ScriptedSession(Session):
    def __init__(self, script, log):
        self.script = list(script)
        self.log = log

    async def step(self, *, allow_tools=True):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def add_results(self, results):
        self.log.extend(results)


class ScriptedProvider(Provider):
    def __init__(self, name, script):
        self.name = name
        self.label = name
        self.script = script
        self.results = []
        self.tools = None

    def session(self, system, history, tools):
        self.system = system
        self.tools = [tool.name for tool in tools]
        return ScriptedSession(self.script, self.results)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.app = _stub_app()
        self.admin = AdminResponse(id=7, username="scorer", is_active=True, permissions=["matches.delete"])

    def _engine(self, mode, providers, admin=None):
        executor = ToolExecutor(self.app, public=mode == "public")
        return Engine(mode, executor, providers, admin=admin), executor

    def test_falls_back_to_the_next_provider(self):
        broken = ScriptedProvider("first", [ProviderError("down")])
        working = ScriptedProvider(
            "second",
            [Reply(calls=[ToolCall("1", "list_seasons", {})]), Reply(text="There is **1** season.")],
        )
        engine, executor = self._engine("public", [broken, working])
        result = run(engine.chat("How many seasons?", []))
        run(executor.close())
        self.assertEqual(result.reply, "There is **1** season.")
        self.assertEqual(result.provider, "second")
        self.assertEqual(result.sources, [{"tool": "list_seasons", "label": "Seasons"}])
        self.assertEqual(len(working.results[0][1]["data"]), 1)

    def test_public_cannot_call_write_tools(self):
        provider = ScriptedProvider(
            "p", [Reply(calls=[ToolCall("1", "delete_match", {"match_id": 10})]), Reply(text="I can't do that.")]
        )
        engine, executor = self._engine("public", [provider])
        result = run(engine.chat("delete match 10", []))
        run(executor.close())
        self.assertNotIn("delete_match", provider.tools)
        self.assertFalse(provider.results[0][1]["ok"])
        self.assertIsNone(result.action)
        self.assertEqual(self.app.state.deleted, [])

    def test_writes_wait_for_confirmation(self):
        provider = ScriptedProvider("p", [Reply(text="Deleting match 10.", calls=[ToolCall("1", "delete_match", {"match_id": 10})])])
        engine, executor = self._engine("admin", [provider], admin=self.admin)
        result = run(engine.chat("delete match 10", []))
        self.assertEqual(self.app.state.deleted, [])
        self.assertIsNotNone(result.action)
        self.assertTrue(result.action["destructive"])
        self.assertEqual(result.action["title"], "Delete match")
        confirmed = run(engine.confirm(result.action["token"]))
        run(executor.close())
        self.assertEqual(self.app.state.deleted, [10])
        self.assertTrue(confirmed.changed)
        self.assertIn("Match deleted successfully", confirmed.reply)

    def test_admin_without_permission_cannot_confirm(self):
        token = actions.sign("create_team", {"name": "Strikers"}, self.admin.id)
        engine, executor = self._engine("admin", [], admin=self.admin)
        result = run(engine.confirm(token))
        run(executor.close())
        self.assertIn("permission", result.reply)
        self.assertFalse(result.changed)

    def test_invalid_write_arguments_go_back_to_the_model(self):
        provider = ScriptedProvider(
            "p",
            [Reply(calls=[ToolCall("1", "delete_match", {"match_id": "abc"})]), Reply(text="Which match?")],
        )
        engine, executor = self._engine("admin", [provider], admin=self.admin)
        result = run(engine.chat("delete it", []))
        run(executor.close())
        self.assertIsNone(result.action)
        self.assertIn("Invalid arguments", provider.results[0][1]["error"])

    def test_no_provider_gives_a_clear_message(self):
        engine, executor = self._engine("public", [])
        result = run(engine.chat("hi", []))
        run(executor.close())
        self.assertIn("isn't switched on", result.reply)
        self.assertFalse(result.available)

    def test_used_up_credits_hide_the_assistant(self):
        provider = ScriptedProvider("groq", [ProviderError("quota", kind="exhausted")])
        engine, executor = self._engine("public", [provider])
        result = run(engine.chat("hi", []))
        run(executor.close())
        self.assertFalse(result.available)
        self.assertIn("No AI provider is available", result.reply)

    def test_admin_prompt_names_the_admin_and_permissions(self):
        provider = ScriptedProvider("p", [Reply(text="hi")])
        engine, executor = self._engine("admin", [provider], admin=self.admin)
        run(engine.chat("hi", []))
        run(executor.close())
        self.assertIn("scorer", provider.system)
        self.assertIn("matches.delete", provider.system)


class RouteTests(unittest.TestCase):
    def _status(self, keys, probe):
        from fastapi.testclient import TestClient

        from core.config import settings
        from main import app
        from services.assistant import readiness

        readiness.clear()

        async def fake_probe(provider):
            return probe(provider)

        with (
            mock.patch.multiple(settings, **keys),
            mock.patch("services.assistant.readiness.probe", fake_probe),
        ):
            return TestClient(app).get("/assistant/status")

    def test_credit_errors_are_exhausted_and_a_rate_limit_is_not(self):
        from services.assistant.providers import failure_kind

        self.assertEqual(failure_kind(429, "Rate limit reached on tokens per day (TPD)"), "exhausted")
        self.assertEqual(failure_kind(402, "This request requires more credits"), "exhausted")
        self.assertEqual(failure_kind(429, "Rate limit reached on requests per minute (RPM)"), "limited")
        self.assertEqual(failure_kind(401, "invalid api key"), "unavailable")
        self.assertEqual(failure_kind(404, "The model `llama-3.3-70b-versatile` does not exist"), "unavailable")

    def test_status_reports_a_provider_that_can_answer(self):
        response = self._status(
            {"GEMINI_API_KEY": "k-secret", "GROQ_API_KEY": None, "OPENROUTER_API_KEY": None, "CURSOR_API_KEY": None},
            lambda _provider: "ok",
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["data"], {"name": "Striker", "available": True, "providers": ["Google Gemini"]})
        self.assertNotIn("k-secret", response.text)

    def test_status_hides_the_assistant_when_credits_are_used_up(self):
        response = self._status(
            {"GEMINI_API_KEY": None, "GROQ_API_KEY": "k-secret", "OPENROUTER_API_KEY": None, "CURSOR_API_KEY": None},
            lambda _provider: "exhausted",
        )
        self.assertEqual(response.json()["data"]["available"], False)
        self.assertEqual(response.json()["data"]["providers"], [])

    def test_status_stays_available_when_one_provider_can_answer(self):
        response = self._status(
            {"GEMINI_API_KEY": "k-secret", "GROQ_API_KEY": "k-secret", "OPENROUTER_API_KEY": None, "CURSOR_API_KEY": None},
            lambda provider: "exhausted" if provider.name == "gemini" else "ok",
        )
        data = response.json()["data"]
        self.assertTrue(data["available"])
        self.assertEqual(data["providers"], ["Groq"])

    def test_public_chat_validates_input(self):
        from fastapi.testclient import TestClient

        from main import app

        response = TestClient(app).post("/assistant/public/chat", json={"message": "x" * 1001})
        self.assertEqual(response.status_code, 422)

    def test_admin_chat_needs_sign_in(self):
        from fastapi.testclient import TestClient

        from main import app
        from routes.auth_routes import get_service

        app.dependency_overrides[get_service] = lambda: None
        try:
            response = TestClient(app).post("/assistant/admin/chat", json={"message": "hi"})
        finally:
            app.dependency_overrides.pop(get_service, None)
        self.assertEqual(response.status_code, 401)

    def test_a_turned_off_assistant_does_not_call_a_provider(self):
        from unittest.mock import AsyncMock, patch

        from fastapi.testclient import TestClient

        from main import app
        from routes import assistant_routes

        with patch.object(assistant_routes, "assistant_is_enabled", new=AsyncMock(return_value=False)):
            response = TestClient(app).post("/assistant/public/chat", json={"message": "Who is leading?"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("turned off", body["data"]["reply"])
        self.assertIsNone(body["data"]["provider"])

    def test_the_switch_is_read_before_any_model_call(self):
        from routes.assistant_routes import assistant_is_enabled

        off = FastAPI()

        @off.get("/settings")
        def settings_off():
            return {"success": True, "message": None, "data": {"assistant_enabled": False}}

        on = FastAPI()

        @on.get("/settings")
        def settings_on():
            return {"success": True, "message": None, "data": {"assistant_enabled": True}}

        unread = FastAPI()

        self.assertFalse(run(assistant_is_enabled(off)))
        self.assertTrue(run(assistant_is_enabled(on)))
        self.assertFalse(run(assistant_is_enabled(unread)))


class CursorProviderTests(unittest.TestCase):
    def test_plain_text_and_tool_json(self):
        from services.assistant.providers import cursor_reply

        plain = cursor_reply("The season has six teams.")
        self.assertEqual(plain.text, "The season has six teams.")
        self.assertEqual(plain.calls, [])
        reply = cursor_reply(
            '```json\n{"tool_calls":[{"name":"list_seasons","arguments":{"season_id":3}}]}\n```'
        )
        self.assertEqual([call.name for call in reply.calls], ["list_seasons"])
        self.assertEqual(reply.calls[0].args, {"season_id": 3})

    def test_cursor_is_last_when_its_key_is_set(self):
        import httpx

        from core.config import settings
        from services.assistant.providers import configured_providers

        with mock.patch.multiple(
            settings,
            GEMINI_API_KEY="g",
            GROQ_API_KEY=None,
            OPENROUTER_API_KEY="o",
            CURSOR_API_KEY="c",
            CURSOR_MODEL=None,
        ):
            names = [provider.name for provider in configured_providers(httpx.AsyncClient())]
        self.assertEqual(names, ["gemini", "openrouter", "cursor"])

    def test_chat_calls_cursor_without_a_repository(self):
        import httpx

        from services.assistant.providers import CursorProvider

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "POST" and request.url.path == "/v1/agents":
                return httpx.Response(
                    200,
                    json={"agent": {"id": "bc-1"}, "run": {"id": "run-1", "status": "CREATING"}},
                )
            if request.method == "GET" and request.url.path == "/v1/agents/bc-1/runs/run-1":
                return httpx.Response(200, json={"status": "FINISHED", "result": "The season has six teams."})
            if request.method == "POST" and request.url.path == "/v1/agents/bc-1/archive":
                return httpx.Response(200, json={"id": "bc-1"})
            return httpx.Response(500, text="unexpected")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = CursorProvider("crsr_test", "composer-2.5", client)
                return await provider.session("You are Striker.", [], []).step()

        reply = run(go())
        self.assertEqual(reply.text, "The season has six teams.")
        created = next(item for item in seen if item.method == "POST" and item.url.path == "/v1/agents")
        body = json.loads(created.content)
        self.assertNotIn("repos", body)
        self.assertEqual(body["mode"], "plan")
        self.assertIs(body["autoCreatePR"], False)
        self.assertEqual(body["model"], {"id": "composer-2.5"})
        self.assertEqual(created.headers["authorization"], "Bearer crsr_test")
        self.assertTrue(any(item.url.path == "/v1/agents/bc-1/archive" for item in seen))

    def test_a_rejected_key_is_unavailable_without_creating_an_agent(self):
        import httpx

        from services.assistant.providers import CursorProvider, ProviderError

        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(401, text="Invalid API key")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await CursorProvider("crsr_test", None, client).check()

        with self.assertRaises(ProviderError) as caught:
            run(go())
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertEqual(paths, ["/v1/me"])


if __name__ == "__main__":
    unittest.main()
