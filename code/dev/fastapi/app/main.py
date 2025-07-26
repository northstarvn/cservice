import uvicorn
from fastapi import FastAPI
from app.routers import users, bookings
from app.db import Base, engine

app = FastAPI(title="CService Booking Backend")

@app.on_event("startup")
async def on_startup():
    # Create all DB tables (in production use Alembic migrations)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(bookings.router, prefix="/bookings", tags=["bookings"])

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)