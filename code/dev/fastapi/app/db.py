import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import logging

# Load environment variables from a .env file if present
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/cservice")

engine = create_async_engine(DATABASE_URL, echo=True)
SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            raise
        finally:
            await session.close()

# Import models here so they're registered with Base
# This ensures tables are created when Base.metadata.create_all() is called
try:
    from models import TestModel  # This will register the model
    logging.info("Models imported successfully")
except ImportError:
    logging.warning("Models not found - create models.py to define your database tables")

# Test connection function
async def test_connection():
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT 1"))
            logging.info("Database connection successful!")
            return True
    except Exception as e:
        logging.error(f"Database connection failed: {e}")
        return False