from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from db import engine, Base, get_db
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        async with engine.begin() as conn:
            # Test connection first
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection verified")
            
            # Create tables
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created successfully")
            
    except Exception as e:
        logger.error(f"Database startup error: {e}", exc_info=True)
        raise
    
    yield  # App runs here
    
    # Shutdown
    await engine.dispose()
    logger.info("Database engine disposed")

app = FastAPI(
    title="CService API",
    description="A FastAPI application with PostgreSQL",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/")
async def root():
    return {"message": "Hello World", "status": "running"}

@app.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    try:
        # Test database connection
        result = await db.execute(text("SELECT version()"))
        db_version = result.scalar()
        
        return {
            "status": "healthy",
            "database": "connected",
            "postgresql_version": db_version,
            "timestamp": "2025-08-10 11:21:40"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e)
        }

@app.get("/db-test")
async def database_test(db: AsyncSession = Depends(get_db)):
    """Test database operations"""
    try:
        # Test a simple query
        result = await db.execute(text("SELECT 'Database is working!' as message, NOW() as timestamp"))
        row = result.fetchone()
        
        return {
            "message": row.message,
            "timestamp": row.timestamp,
            "status": "success"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}