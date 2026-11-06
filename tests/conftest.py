import time

import pytest
from alembic import command
from alembic.config import Config

from trip_coordinator.contracts import RuntimeConfig
from trip_coordinator.service import Coordinator, seed
from trip_coordinator.storage import make_engine


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / "trip.db")
    migration = Config("alembic.ini")
    migration.set_main_option("trip_database_path", path)
    command.upgrade(migration, "head")
    engine = make_engine(path)
    seed(engine)
    config = RuntimeConfig(
        bridge_url="http://127.0.0.1:8000",
        room_id="room",
        bridge_id="telegram",
        coordinator_agent_id="coordinator-id",
        conversation_enabled=True,
        group_privacy=False,  # Legacy shared workflows; separate tests enable privacy.
        planner_participant_id="planner",
        reservation_participant_id="reservation",
        database_path=path,
        members=[
            {"participant_id": "parent", "can_approve": True},
            {"participant_id": "child", "can_approve": False},
        ],
    )
    now = [int(time.time())]
    coordinator = Coordinator(engine, config, lambda: now[0])
    yield engine, config, now, coordinator
    engine.dispose()
