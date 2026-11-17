from pipeline_incident_copilot.rag.ingestion.chunking import chunk
from pipeline_incident_copilot.rag.ingestion.loaders import PIPELINE_NOTEBOOKS_DIR
from pipeline_incident_copilot.rag.ingestion.loaders.notebook_loader import NotebookLoader


def test_structure_strategy_gives_one_parent_per_task_notebook_and_tags_sections():
    chunks = chunk(NotebookLoader(PIPELINE_NOTEBOOKS_DIR).load(), "structure")
    parents = {c.metadata["parent_id"] for c in chunks}
    assert {"code:bronze_ingest", "code:silver_parse", "code:quality_checks", "code:gold_aggregate"} <= parents
    silver_sections = {c.metadata["section"] for c in chunks if c.metadata["parent_id"] == "code:silver_parse"}
    assert {"parse", "write-silver"} <= silver_sections


def test_chunk_ids_are_unique_and_prefixed_by_strategy():
    chunks = chunk(NotebookLoader(PIPELINE_NOTEBOOKS_DIR).load(), "fixed")
    ids = [c.metadata["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids)) and all(i.startswith("fixed-") for i in ids)
