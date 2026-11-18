import asyncio
import logging

import pyodbc
from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware

from core.admin_seed import ensure_seed_admin
from core.database import Database
from repositories.permission_repository import PermissionRepository
from core.config import settings
from core.exceptions import add_exception_handlers
from core.realtime import PublicChangeMiddleware
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
from routes.media_routes import router as media_router
from routes.gallery_routes import router as gallery_router
from routes.assistant_routes import router as assistant_router
from routes.realtime_routes import router as realtime_router
from routes.notify_routes import router as notify_router
from services.notify.flags import load_flags
from services.notify.redact import install_feed_token_filter
from services.notify.worker import run_notify_worker

install_feed_token_filter()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Database.initialize()
    connection = Database.get_connection()
    try:
        try:
            PermissionRepository(connection).sync_catalog()
        except pyodbc.Error:
            connection.rollback()
            logging.getLogger(__name__).exception(
                "Permission catalog sync failed. Apply the database schema that adds tblPermissions."
            )
        ensure_seed_admin(connection)
    finally:
        connection.close()
    stop_notify = asyncio.Event()
    notify_task = None
    flags = load_flags()
    if flags.worker_enabled and flags.delivery_ready():
        notify_task = asyncio.create_task(run_notify_worker(stop_notify))
    yield
    if notify_task is not None:
        stop_notify.set()
        notify_task.cancel()
        try:
            await notify_task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=lifespan)
add_exception_handlers(app)
app.add_middleware(PublicChangeMiddleware)
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
app.include_router(media_router)
app.include_router(gallery_router)
app.include_router(assistant_router)
app.include_router(realtime_router)
app.include_router(notify_router)