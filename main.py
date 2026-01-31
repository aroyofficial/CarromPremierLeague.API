from fastapi import FastAPI
from core.database import Database

app = FastAPI()

@app.on_event("startup")
def startup():
    Database.initialize()