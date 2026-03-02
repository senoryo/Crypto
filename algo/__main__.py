"""Entry point for the algo engine: python -m algo"""

import asyncio
import signal

from algo.engine import AlgoEngine
from shared.logging_config import setup_component_logging

logger = setup_component_logging("ALGO")


async def main():
    engine = AlgoEngine()

    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("Received shutdown signal")
        asyncio.ensure_future(engine.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: _signal_handler())

    try:
        await engine.start()
    except KeyboardInterrupt:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
