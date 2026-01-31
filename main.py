from fastapi import FastAPI
from core.database import Database
from routes.season_routes import router as season_router  
  
app = FastAPI()

@app.on_event("startup")
def startup():
    Database.initialize()

app.include_router(season_router)