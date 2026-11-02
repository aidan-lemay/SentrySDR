from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sentrysdr import security
from sentrysdr.config import Config
from sentrysdr.context import Context
from sentrysdr.models import User
from sentrysdr.web import create_app

ADMIN_EMAIL = "admin@example.com"
PASSWORD = "correct-horse-battery"
HEADERS = {"X-SentrySDR": "1"}


def make_config(tmp_path, **overrides) -> Config:
    values = dict(
        data_dir=tmp_path / "data", recordings_dir=tmp_path / "rec", log_dir=tmp_path / "logs",
        serials=["SER1", "SER2"], public_url="https://sentry.example.com",
        hang_seconds=0.3, min_call_seconds=0.1,
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def ctx(tmp_path):
    return Context(make_config(tmp_path), enumerate_fn=lambda: ["SER1", "SER2"])


@pytest.fixture
def app(ctx):
    return create_app(ctx, run_background=False)


def add_user(ctx, email, role="listener", status="active", password=PASSWORD, **fields):
    with ctx.db() as session:
        user = User(email=email, name=email.split("@")[0], password_hash=security.hash_password(password),
                    role=role, status=status, **fields)
        session.add(user)
        session.commit()
        return user.id


def client_for(app, email=None, password=PASSWORD):
    client = TestClient(app, base_url="https://sentry.example.com", headers=HEADERS)
    if email:
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
    return client


@pytest.fixture
def admin(ctx, app):
    add_user(ctx, ADMIN_EMAIL, role="admin")
    return client_for(app, ADMIN_EMAIL)


@pytest.fixture
def operator(ctx, app):
    add_user(ctx, "op@example.com", role="operator")
    return client_for(app, "op@example.com")


@pytest.fixture
def listener(ctx, app):
    add_user(ctx, "listen@example.com", role="listener")
    return client_for(app, "listen@example.com")
