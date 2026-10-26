"""Apply every sql/*.sql file in name order. All DDL is idempotent, so re-running is safe."""

from core.config import ROOT
from core.db import connect
from core.log import get_logger

log = get_logger("migrate")


def main() -> None:
    files = sorted((ROOT / "sql").glob("*.sql"))
    with connect() as conn:
        for path in files:
            conn.execute(path.read_text())
            log.info("applied %s", path.name)
    log.info("migrations complete (%d files)", len(files))


if __name__ == "__main__":
    main()
