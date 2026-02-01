from fastapi import FastAPI
from contextlib import asynccontextmanager

from core.database import Database
from core.exceptions import add_exception_handlers
from routes.season_routes import router as season_router
from routes.team_routes import router as team_router
from routes.player_routes import router as player_router
from routes.country_routes import router as country_router
from routes.stats_routes import router as stats_router
from routes.roster_routes import router as roster_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    Database.initialize()
    yield

app = FastAPI(lifespan=lifespan)
add_exception_handlers(app)

app.include_router(season_router)
app.include_router(team_router)
app.include_router(player_router)
app.include_router(country_router)
app.include_router(stats_router)
app.include_router(roster_router)