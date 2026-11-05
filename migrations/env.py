from alembic import context

from trip_coordinator.storage import make_engine, metadata

engine = make_engine(context.config.get_main_option("trip_database_path", "data/trips.db"))
try:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
