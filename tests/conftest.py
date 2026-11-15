import pytest

from backend.db import connect


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "memory.db")
    yield connection
    connection.close()
