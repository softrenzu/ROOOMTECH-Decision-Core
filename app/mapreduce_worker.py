from __future__ import annotations

import asyncio
from dotenv import load_dotenv

load_dotenv()

from app.distributed import RedisDistributedMapReduce
from app.engine import DecisionEngine
from app.mapreduce import MapReduceEngine
from app.operations import OperationalDecisionEngine
from app.schema_extraction import SchemaExtractor


async def main():
    decision = DecisionEngine()
    operations = OperationalDecisionEngine(decision)
    extractor = SchemaExtractor(decision.model)
    engine = MapReduceEngine(decision, operations, extractor)
    worker = RedisDistributedMapReduce(engine)
    await worker.worker_loop()


if __name__ == "__main__":
    asyncio.run(main())
