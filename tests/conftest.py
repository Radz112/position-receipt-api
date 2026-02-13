import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.middleware.rate_limit import reset_rate_limits


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    """Reset rate limiter state before each test to prevent cross-test pollution."""
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
