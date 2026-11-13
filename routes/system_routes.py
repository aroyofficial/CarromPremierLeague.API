from html import escape
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from core.config import settings


router = APIRouter(tags=["Service"])

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

GROUP_ORDER = [
    "Service",
    "Authentication",
    "Seasons",
    "Teams",
    "Players",
    "Countries",
    "Matches",
    "Statistics",
    "Roster",
    "Documentation",
]

METHOD_ORDER = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}

DESCRIPTIONS = {
    ("GET", "/"): "HTML index of every registered endpoint",
    ("GET", "/health"): "HTML page showing the API service is live",
    ("POST", "/auth/login"): "Authenticate an admin and receive a bearer token",
    ("POST", "/auth/bootstrap"): "Create the first admin; only available on an empty admin table",
    ("GET", "/auth/me"): "Get the authenticated admin",
    ("GET", "/admins"): "List admin users",
    ("GET", "/admins/{admin_id}"): "Get an admin user",
    ("POST", "/admins"): "Create an admin user",
    ("PATCH", "/admins/{admin_id}"): "Update an admin user",
    ("DELETE", "/admins/{admin_id}"): "Delete an admin user",
    ("GET", "/docs"): "Interactive API documentation",
    ("GET", "/redoc"): "ReDoc API documentation",
    ("GET", "/openapi.json"): "OpenAPI schema",
    ("GET", "/countries/"): "List countries",
    ("GET", "/countries/{country_id}"): "Get a country by id",
    ("POST", "/countries/"): "Create a country",
    ("PATCH", "/countries/{country_id}"): "Update a country",
    ("DELETE", "/countries/{country_id}"): "Delete a country",
    ("GET", "/teams/"): "List teams",
    ("GET", "/teams/{team_id}"): "Get a team by id",
    ("POST", "/teams/"): "Create a team",
    ("PATCH", "/teams/{team_id}"): "Update a team",
    ("DELETE", "/teams/{team_id}"): "Delete a team",
    ("GET", "/players/"): "List players",
    ("GET", "/players/profiles"): "List player profiles and season totals",
    ("GET", "/players/{player_id}"): "Get a player by id",
    ("POST", "/players/"): "Create a player",
    ("PATCH", "/players/{player_id}"): "Update a player",
    ("DELETE", "/players/{player_id}"): "Delete a player",
    ("GET", "/seasons/"): "List seasons",
    ("GET", "/seasons/{season_id}"): "Get a season by id",
    ("POST", "/seasons/"): "Create a season",
    ("PUT", "/seasons/{season_id}"): "Update a season",
    ("DELETE", "/seasons/{season_id}"): "Delete a season",
    ("GET", "/seasons/{season_id}/teams"): "List teams taking part in a season",
    ("POST", "/seasons/{season_id}/teams"): "Add a team to a season",
    ("DELETE", "/seasons/{season_id}/teams/{team_id}"): "Remove a team from a season",
    ("GET", "/seasons/{season_id}/schedule"): "Get the match schedule for a season",
    ("GET", "/seasons/{season_id}/league-table"): "Get the league table for a season",
    ("GET", "/settings"): "Get competition settings",
    ("PUT", "/settings"): "Update competition settings",
    ("GET", "/matches"): "List matches",
    ("GET", "/matches/next-order"): "Get the next match order for a season",
    ("GET", "/matches/{match_id}"): "Get a match by id",
    ("POST", "/matches"): "Create a match",
    ("PATCH", "/matches/{match_id}"): "Update a match",
    ("GET", "/matches/{match_id}/stats"): "Get player stats for a match",
    ("PUT", "/matches/{match_id}/stats"): "Save player stats for a match",
    ("DELETE", "/matches/{match_id}"): "Delete a match",
    ("GET", "/stats/head-to-head"): "Head-to-head record for two teams",
    ("GET", "/stats/season/{season_id}/top-coin-potters"): "Top coin potters in a season",
    ("GET", "/stats/season/{season_id}/players"): "Player statistics for a season",
    ("POST", "/rosters/assign"): "Assign a player to a team for a season",
    ("DELETE", "/rosters/remove"): "Remove a player from a season roster",
    ("GET", "/rosters/team/{team_id}"): "Get a team roster for a season",
    ("GET", "/rosters/player/{player_id}"): "Get a player's season history",
    ("GET", "/rosters/season/{season_id}"): "Get every roster in a season",
    ("GET", "/rosters/season/{season_id}/teams"): "Get team player history for a season",
}

DOC_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _render(template_name: str, **values: str) -> str:
    css = (STATIC_DIR / "base.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / template_name).read_text(encoding="utf-8")
    html = html.replace("{{BASE_CSS}}", css)
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", value)
    return html


def _page_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}


def _query_info(field) -> dict:
    name = getattr(field, "alias", None) or field.name
    default = getattr(field, "default", None)
    if type(default).__name__ == "PydanticUndefinedType":
        return {"name": name, "required": True, "default": None}
    return {"name": name, "required": False, "default": default}


def _query_token(query: dict) -> str:
    if query["default"] is None:
        return escape(query["name"])
    return f'{escape(query["name"])}={escape(str(query["default"]))}'


def _query_note(query: dict) -> str:
    if query["required"]:
        return f'{query["name"]} required'
    if query["default"] is None:
        return f'{query["name"]} optional'
    return f'{query["name"]} optional, default {query["default"]}'


def _path_sort_key(path: str) -> str:
    return path.replace("{", "~")


def _description(method: str, path: str, name: str) -> str:
    text = DESCRIPTIONS.get((method, path))
    if text:
        return text
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else path


def _group_name(route) -> str:
    if route.path in DOC_PATHS:
        return "Documentation"
    tags = getattr(route, "tags", None) or []
    if tags:
        return str(tags[0])
    return "Other"


def _iter_routes(app):
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            for child in original.routes:
                if isinstance(child, APIRoute):
                    yield child
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path in DOC_PATHS and methods:
            yield route


def _collect(app) -> list[dict]:
    rows = []
    for route in _iter_routes(app):
        if route.path == "/docs/oauth2-redirect":
            continue
        methods = sorted(
            (method for method in route.methods if method not in {"HEAD", "OPTIONS"}),
            key=lambda method: METHOD_ORDER.get(method, 9),
        )
        if not methods:
            continue
        dependant = getattr(route, "dependant", None)
        query = [_query_info(field) for field in dependant.query_params] if dependant else []
        has_body = bool(dependant.body_params) if dependant else False
        protected = bool(dependant and any(
            getattr(dependency.call, "__name__", "") == "get_current_admin"
            for dependency in dependant.dependencies
        ))
        for method in methods:
            rows.append({
                "group": _group_name(route),
                "method": method,
                "path": route.path,
                "query": query,
                "body": has_body,
                "protected": protected,
                "description": _description(method, route.path, route.name),
            })
    rows.sort(key=lambda row: (
        GROUP_ORDER.index(row["group"]) if row["group"] in GROUP_ORDER else len(GROUP_ORDER),
        row["group"],
        _path_sort_key(row["path"]),
        METHOD_ORDER.get(row["method"], 9),
    ))
    return rows


def _render_groups(rows: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)

    blocks = []
    for name, items in groups.items():
        endpoints = []
        for item in items:
            method = item["method"].lower()
            query = ""
            if item["query"]:
                query = f'<span class="query">?{"&".join(_query_token(part) for part in item["query"])}</span>'
            notes = [_query_note(part) for part in item["query"]]
            if item["protected"]:
                notes.append("Bearer token required")
            if item["body"]:
                notes.append("JSON body")
            note_html = ""
            if notes:
                note_html = f'<p class="notes">{escape(" · ".join(notes))}</p>'
            endpoints.append(
                "<li class=\"endpoint\">"
                f'<span class="method method-{method}">{escape(item["method"])}</span>'
                f'<code class="path">{escape(item["path"])}{query}</code>'
                f'<p class="desc">{escape(item["description"])}</p>'
                f"{note_html}"
                "</li>"
            )
        label = "route" if len(items) == 1 else "routes"
        blocks.append(
            "<section class=\"group\">"
            "<header>"
            f"<h2>{escape(name)}</h2>"
            f"<span>{len(items)} {label}</span>"
            "</header>"
            f'<ul class="endpoints">{"".join(endpoints)}</ul>'
            "</section>"
        )
    return "".join(blocks)


@router.get("/", response_class=HTMLResponse, summary="Endpoint index")
def endpoint_index(request: Request):
    rows = _collect(request.app)
    count = len(rows)
    noun = "route" if count == 1 else "routes"
    html = _render(
        "index.html",
        APP_NAME=escape(settings.APP_NAME),
        APP_ENV=escape(settings.APP_ENV),
        ENDPOINT_SUMMARY=escape(
            f"{count} {noun}, grouped by resource. Paths are shown as they are registered."
        ),
        GROUPS=_render_groups(rows),
    )
    return HTMLResponse(html, headers=_page_headers())


@router.get("/health", response_class=HTMLResponse, summary="Health page")
def health_page():
    html = _render(
        "health.html",
        APP_NAME=escape(settings.APP_NAME),
        APP_ENV=escape(settings.APP_ENV),
    )
    return HTMLResponse(html, headers=_page_headers())
