from fastapi import FastAPI
from core.database import Database
from routes.season_routes import router as season_router
from routes.team_routes import router as team_router
  
app = FastAPI()

@app.on_event("startup")
def startup():
    Database.initialize()

app.include_router(season_router)
app.include_router(team_router)