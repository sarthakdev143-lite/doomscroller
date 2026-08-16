"""Initialize the database (create all tables)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import init_db
from app.config import settings


def main() -> None:
    print(f"DB URL: {settings.db_url}")
    init_db()
    print("Database initialized.")


if __name__ == "__main__":
    main()
