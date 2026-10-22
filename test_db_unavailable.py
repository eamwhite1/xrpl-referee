"""Test that the app starts and serves critical endpoints even when DB is unreachable."""
import asyncio
import os
import time

os.environ.setdefault("DATABASE_URL", "postgresql://invalid:invalid@localhost:1/nonexistent")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt")


def test_health_without_db():
    from httpx import AsyncClient, ASGITransport
    from referee import app

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            t0 = time.monotonic()
            r = await client.get("/health")
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"Health took {elapsed:.1f}s"
            assert r.status_code == 200
            data = r.json()
            assert data.get("db") in ("ok", "unavailable"), f"Unexpected db field: {data}"

            r2 = await client.get("/status")
            assert r2.status_code in (200, 204)

    asyncio.run(run())


if __name__ == "__main__":
    test_health_without_db()
    print("✅ DB-unavailable startup test passed")
