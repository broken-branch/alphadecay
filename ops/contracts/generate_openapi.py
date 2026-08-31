from __future__ import annotations

import json
from pathlib import Path

from backend.app.main import app

OUTPUT = Path("contracts/openapi-v1.json")


def main() -> None:
    document = json.dumps(app.openapi(), indent=2, sort_keys=True)
    OUTPUT.write_text(f"{document}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
