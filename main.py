from fastapi import FastAPI
from core.database import Database
from routes.season_routes import router as season_router
from routes.team_routes import router as team_router
from routes.player_routes import router as player_router
from routes.country_routes import router as country_router
  
app = FastAPI()

@app.on_event("startup")
def startup():
    Database.initialize()

app.include_router(season_router)
app.include_router(team_router)
app.include_router(player_router)
app.include_router(country_router)