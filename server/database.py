"""SQLAlchemy engine and request-session helpers."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import PROJECT_DIR, Settings


class Database:
    def __init__(self, database_url: str):
        connect_args = (
            {"check_same_thread": False}
            if database_url.startswith("sqlite")
            else {}
        )
        self.engine = create_engine(
            database_url, connect_args=connect_args, pool_pre_ping=True
        )
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def create_schema(self) -> None:
        """Test/development helper. Production startup should run Alembic."""
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


_database: Database | None = None


def configure_database(settings: Settings | None = None) -> Database:
    global _database
    if _database is not None:
        _database.dispose()
    _database = Database((settings or Settings.from_env()).database_url)
    return _database


def upgrade_database(settings: Settings | None = None) -> None:
    """Apply Alembic migrations before the application opens its engine."""
    from alembic import command
    from alembic.config import Config

    resolved = settings or Settings.from_env()
    config_path = PROJECT_DIR / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", resolved.database_url)
    probe = create_engine(resolved.database_url)
    try:
        tables = set(inspect(probe).get_table_names())
        versions = []
        if "alembic_version" in tables:
            with probe.connect() as connection:
                versions = list(
                    connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalars()
                )
    finally:
        probe.dispose()
    managed_tables = {
        "sessions",
        "check_runs",
        "order_results",
        "file_results",
        "correction_decisions",
        "run_events",
        "order_actions",
    }
    if managed_tables.issubset(tables) and not versions:
        # Baseline databases created by the short-lived pre-Alembic web build.
        # Stamp only after verifying the complete initial schema is present.
        baseline = create_engine(resolved.database_url)
        try:
            with baseline.begin() as connection:
                if "alembic_version" not in tables:
                    connection.exec_driver_sql(
                        "CREATE TABLE alembic_version "
                        "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                    )
                connection.exec_driver_sql(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('0001_server_foundation')"
                )
        finally:
            baseline.dispose()
    command.upgrade(config, "head")


def current_database() -> Database:
    global _database
    if _database is None:
        _database = Database(Settings.from_env().database_url)
    return _database


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding one transactional unit of work."""
    with current_database().session_factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_engine() -> Engine:
    return current_database().engine
