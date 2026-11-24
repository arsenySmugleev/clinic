import asyncio
import os
import socket
import subprocess
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from infrastructure.cache import RedisCache, get_cache
from src.application import get_app
from src.clients.reviews import ReviewsClient
from src.db import get_session, get_uow
from src.dependencies import get_reviews_client
from src.services.doctor_appointment import DoctorAppointmentService
from src.services.med_card_insurance import MedCardInsuranceService
from src.services.patient_med_service import PatientMedServiceService
from src.uow import UnitOfWork

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWS_ROOT = PROJECT_ROOT.parent / "reviews"
REVIEWS_PYTHON = REVIEWS_ROOT / ".venv" / "Scripts" / "python.exe"


def _alembic_config(db_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def _current_revision(sync_connection: Connection) -> str | None:
    return MigrationContext.configure(sync_connection).get_current_revision()


async def _get_current_revision(db_url: str) -> str | None:
    engine = create_async_engine(db_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_current_revision)
    finally:
        await engine.dispose()


def reset_database(db_url: str) -> None:
    config = _alembic_config(db_url)
    current = asyncio.run(_get_current_revision(db_url))
    if current is not None:
        command.downgrade(config, "base")
    command.upgrade(config, "head")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_reviews_healthy(base_url: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthcheck", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Reviews service did not become healthy at {base_url}")


def _upgrade_reviews_database(db_url: str) -> None:
    if not REVIEWS_PYTHON.exists():
        raise RuntimeError(f"Reviews venv not found: {REVIEWS_PYTHON}")
    env = os.environ.copy()
    env["postgres_url"] = db_url
    subprocess.run(
        [str(REVIEWS_PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=REVIEWS_ROOT,
        env=env,
        check=True,
    )


def make_reviews_client(
    base_url: str,
    *,
    max_attempts: int = 2,
    timeout_seconds: float = 2.0,
    http_client: httpx.AsyncClient | None = None,
) -> ReviewsClient:
    client = http_client or httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=timeout_seconds,
    )
    return ReviewsClient(
        base_url,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_base_seconds=0.05,
        backoff_jitter_seconds=0.01,
        backoff_exp_base=2.0,
        client=client,
    )


def make_unavailable_reviews_client() -> ReviewsClient:
    return make_reviews_client(
        f"http://127.0.0.1:{_free_port()}",
        max_attempts=1,
        timeout_seconds=0.3,
    )


def make_permanent_error_reviews_client() -> ReviewsClient:
    stub = FastAPI()

    @stub.post("/api/v1/reviews")
    async def create_review() -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": "bad request"})

    @stub.get("/api/v1/doctors/{doctor_id}/reviews")
    async def list_reviews(doctor_id: str) -> list:
        return []

    transport = ASGITransport(app=stub)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://reviews-stub")
    return make_reviews_client(
        "http://reviews-stub",
        max_attempts=1,
        http_client=http_client,
    )


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:14") as container:
        yield container


@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer("redis:7-alpine") as container:
        yield container


@pytest.fixture(scope="session")
def postgres_url(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url()
    return url.replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}"


@pytest.fixture(scope="session")
def reviews_postgres_container():
    with PostgresContainer("postgres:14") as container:
        yield container


@pytest.fixture(scope="session")
def reviews_postgres_url(reviews_postgres_container: PostgresContainer) -> str:
    url = reviews_postgres_container.get_connection_url()
    return url.replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def reviews_base_url(reviews_postgres_url: str) -> Iterator[str]:
    _upgrade_reviews_database(reviews_postgres_url)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["postgres_url"] = reviews_postgres_url
    process = subprocess.Popen(
        [
            str(REVIEWS_PYTHON),
            "-m",
            "uvicorn",
            "src.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REVIEWS_ROOT,
        env=env,
    )
    try:
        _wait_reviews_healthy(base_url)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
async def reviews_client(reviews_base_url: str) -> AsyncGenerator[ReviewsClient, None]:
    client = make_reviews_client(reviews_base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def engine(postgres_url: str) -> AsyncGenerator[AsyncEngine, None]:
    await asyncio.to_thread(reset_database, postgres_url)
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def redis(redis_url: str) -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def cache(redis: Redis) -> RedisCache:
    return RedisCache(redis, 3600)


@pytest.fixture
def doctor_service(
    session: AsyncSession,
    cache: RedisCache,
    reviews_client: ReviewsClient,
) -> DoctorAppointmentService:
    return DoctorAppointmentService(UnitOfWork(session), cache, reviews_client)


@pytest.fixture
def patient_service(session: AsyncSession, cache: RedisCache) -> PatientMedServiceService:
    return PatientMedServiceService(session, cache)


@pytest.fixture
def med_card_service(session: AsyncSession, cache: RedisCache) -> MedCardInsuranceService:
    return MedCardInsuranceService(session, cache)


@pytest.fixture
async def client(
    session: AsyncSession,
    cache: RedisCache,
    reviews_client: ReviewsClient,
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    async def override_get_uow() -> AsyncGenerator[UnitOfWork, None]:
        yield UnitOfWork(session)

    async def override_get_cache() -> RedisCache:
        return cache

    def override_get_reviews_client() -> ReviewsClient:
        return reviews_client

    app = get_app()
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_cache] = override_get_cache
    app.dependency_overrides[get_reviews_client] = override_get_reviews_client

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()
