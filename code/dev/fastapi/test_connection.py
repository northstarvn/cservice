import asyncio
import os
import pytest
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

@pytest.mark.asyncio
async def test_direct_asyncpg():
    """Test asyncpg directly using .env DATABASE_URL"""
    try:
        # Get DATABASE_URL from .env and parse it for direct asyncpg connection
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            print("✗ DATABASE_URL not found in .env file")
            return False
            
        print(f"Using DATABASE_URL from .env: {database_url}")
        
        # Parse the URL for asyncpg (remove the +asyncpg part)
        # postgresql+asyncpg://user:pass@host:port/db -> postgresql://user:pass@host:port/db
        direct_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
        
        conn = await asyncpg.connect(direct_url)
        result = await conn.fetchval('SELECT version()')
        print(f"✓ Direct asyncpg connection successful")
        print(f"PostgreSQL version: {result}")
        await conn.close()
        return True
    except Exception as e:
        print(f"✗ Direct asyncpg failed: {e}")
        return False

@pytest.mark.asyncio
async def test_sqlalchemy_engine():
    """Test SQLAlchemy with asyncpg using .env DATABASE_URL"""
    try:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            print("✗ DATABASE_URL not found in .env file")
            return False
            
        print(f"Testing SQLAlchemy with URL from .env: {database_url}")
        
        engine = create_async_engine(database_url, echo=False)
        
        async with engine.begin() as conn:
            # FIXED: Use text() wrapper for raw SQL in SQLAlchemy 2.0+
            result = await conn.execute(text("SELECT 1 as test"))
            row = result.fetchone()
            print(f"✓ SQLAlchemy connection successful: {row}")
        
        await engine.dispose()
        return True
    except Exception as e:
        print(f"✗ SQLAlchemy failed: {e}")
        print(f"Error type: {type(e).__name__}")
        return False

@pytest.mark.asyncio
async def test_env_loading():
    """Test if .env file is loaded correctly"""
    try:
        database_url = os.getenv("DATABASE_URL")
        environment = os.getenv("ENVIRONMENT", "not_set")
        
        print(f"DATABASE_URL loaded: {'✓ Yes' if database_url else '✗ No'}")
        if database_url:
            # Hide password in output for security
            safe_url = database_url.replace(database_url.split('@')[0].split(':')[-1], "***")
            print(f"DATABASE_URL: {safe_url}")
        print(f"ENVIRONMENT: {environment}")
        
        return database_url is not None
    except Exception as e:
        print(f"✗ Environment loading failed: {e}")
        return False

async def main():
    print("=== Database Connection Tests (Using .env) ===")
    print(f"Current working directory: {os.getcwd()}")
    print(f"Looking for .env file at: {os.path.join(os.getcwd(), '.env')}")
    print(f".env file exists: {'✓ Yes' if os.path.exists('.env') else '✗ No'}")
    
    print("\n0. Testing environment loading...")
    env_success = await test_env_loading()
    
    if not env_success:
        print("\n❌ Cannot proceed without DATABASE_URL in .env file")
        print("Please ensure you have a .env file with DATABASE_URL=postgresql+asyncpg://postgres:thoidiMa4@localhost:5432/cservice")
        return
    
    print("\n1. Testing direct asyncpg connection...")
    direct_success = await test_direct_asyncpg()
    
    print("\n2. Testing SQLAlchemy with asyncpg...")
    sqlalchemy_success = await test_sqlalchemy_engine()
    
    print("\n=== Results ===")
    print(f"Environment loading: {'✓ Success' if env_success else '✗ Failed'}")
    print(f"Direct asyncpg: {'✓ Success' if direct_success else '✗ Failed'}")
    print(f"SQLAlchemy: {'✓ Success' if sqlalchemy_success else '✗ Failed'}")
    
    if env_success and direct_success and sqlalchemy_success:
        print("\n🎉 All tests passed! Your database configuration is working correctly.")
    else:
        print("\n💡 Some tests failed. Check the error messages above for troubleshooting.")

if __name__ == "__main__":
    asyncio.run(main())