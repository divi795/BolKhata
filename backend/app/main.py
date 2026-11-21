from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.database import Base, engine
import app.models  # Registers SQLAlchemy models with Base.metadata
from app.routers import customers, transactions, voice


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all tables are created in SQLite
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="BolKhata API",
    description="Voice-powered digital ledger for Indian kirana shops",
    version="0.1.0",
    lifespan=lifespan,
)

# Also ensure tables exist on module import (e.g. for synchronous test runners)
Base.metadata.create_all(bind=engine)


@app.get("/health")
def health_check():
    return {"status": "healthy", "project": "BolKhata"}


app.include_router(customers.router)
app.include_router(transactions.router)
app.include_router(voice.router)
