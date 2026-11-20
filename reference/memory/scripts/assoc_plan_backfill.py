"""Process pending evidence-backed plan states using the existing state directory."""
import argparse,asyncio,json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
async def main(limit):
    from utils import load_config
    from bucket_manager import BucketManager
    from memory_moments import MemoryMomentStore
    from raw_events import RawEventStore
    from embedding_engine import EmbeddingEngine
    from dehydrator import Dehydrator
    from assoc_index import AssocIndex
    from assoc_plans import refresh_plan_states
    c=load_config()
    index=AssocIndex(c,MemoryMomentStore(c),BucketManager(c),RawEventStore(c),EmbeddingEngine(c),Dehydrator(c))
    result=await refresh_plan_states(index,limit)
    print(json.dumps(result));return int(bool(result.get('failed')))
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,default=24)
    sys.exit(asyncio.run(main(max(0,parser.parse_args().limit))))
