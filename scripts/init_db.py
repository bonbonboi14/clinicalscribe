from config import load_config
from storage import initialize_database


if __name__ == "__main__":
    settings = load_config()
    initialize_database(
        settings.database.path,
        busy_timeout_ms=settings.database.busy_timeout_ms,
        journal_mode=settings.database.journal_mode,
        foreign_keys=settings.database.foreign_keys,
    )
    print(f"Initialized database: {settings.database.path}")

