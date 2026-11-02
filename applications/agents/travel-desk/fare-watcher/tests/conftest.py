"""A throwaway Postgres in Docker for the whole test session (tests that need a database skip without Docker)."""
import shutil
import subprocess
import time
import uuid

import pytest


def _docker_ok() -> bool:
    return shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="session")
def postgres_url():
    if not _docker_ok():
        pytest.skip("Docker is not available")
    name = f"desk-test-pg-{uuid.uuid4().hex[:6]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=postgres",
                    "-p", "127.0.0.1::5432", "postgres:16-alpine"], check=True, capture_output=True)
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
                              ).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        for _ in range(60):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        time.sleep(1)  # pg_isready can answer during the init restart
        yield f"postgresql://postgres:postgres@127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
