"""Build Chroma + BM25 indexes for each chunking strategy.

Usage: python -m scripts.build_index [strategy ...]   (default: all)
"""

import sys
import time

from pipeline_incident_copilot.rag.ingestion.chunking import STRATEGIES
from pipeline_incident_copilot.rag.ingestion.ingestion_pipeline import build_index

for strategy in sys.argv[1:] or list(STRATEGIES):
    t = time.time()
    n = build_index(strategy)
    print(f"{strategy}: {n} chunks indexed in {time.time() - t:.0f}s")
