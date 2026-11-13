from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware

from core.admin_seed import ensure_seed_admin
from core.database import Database
from core.config import settings
from core.exceptions import add_exception_handlers
from routes.system_routes import router as system_router
from routes.auth_routes import router as auth_router
from routes.season_routes import router as season_router
from routes.team_routes import router as team_router
from routes.player_routes import router as player_router
from routes.country_routes import router as country_router
from routes.stats_routes import router as stats_router
from routes.roster_routes import router as roster_router
from routes.match_routes import router as match_router
from routes.settings_routes import router as settings_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    Database.initialize()
    connection = Database.get_connection()
    try:
        ensure_seed_admin(connection)
    finally:
        connection.close()
    yield

app = FastAPI(lifespan=lifespan)
add_exception_handlers(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.APP_ALLOWED_HOSTS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system_router)
app.include_router(auth_router)
app.include_router(season_router)
app.include_router(team_router)
app.include_router(player_router)
app.include_router(country_router)
app.include_router(stats_router)
app.include_router(roster_router)
app.include_router(match_router)
app.include_router(settings_router)