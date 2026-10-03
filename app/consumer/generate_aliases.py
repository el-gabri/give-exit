"""``python -m app.consumer.generate_aliases``: see ``app.consumer.alias_generation``."""

import asyncio

from app.consumer.alias_generation import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
