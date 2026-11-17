from pipeline_incident_copilot.rag.ingestion.chunking.markdown_section_chunker import chunk_markdown
from pipeline_incident_copilot.rag.ingestion.chunking.token_counter import CHUNK_TOKEN_BUDGET, count_tokens
from pipeline_incident_copilot.rag.ingestion.loaders import KNOWLEDGE_DIR
from pipeline_incident_copilot.rag.ingestion.loaders.markdown_document_loader import (
    MarkdownDocumentLoader,
    split_front_matter,
)


def test_front_matter_becomes_metadata():
    meta, body = split_front_matter("---\ntitle: X\nfailure_class: permissions\n---\n# X\nbody")
    assert meta == {"title": "X", "failure_class": "permissions"} and body.startswith("# X")


def test_runbook_chunks_share_one_parent_and_carry_a_context_header():
    doc = next(
        d
        for d in MarkdownDocumentLoader(KNOWLEDGE_DIR / "runbooks", "runbook").load()
        if "missing" in d.metadata["file"]
    )
    chunks = chunk_markdown(doc)
    assert {c.metadata["parent_id"] for c in chunks} == {"runbook:missing-input-data"}
    assert all(c.page_content.startswith("[runbook] Missing input data › ") for c in chunks)
    assert {"diagnosis", "resolution"} <= {c.metadata["section"] for c in chunks}


def test_architecture_sections_are_separate_parents_within_token_budget():
    doc = MarkdownDocumentLoader(KNOWLEDGE_DIR / "architecture", "architecture", parent_per_section=True).load()[0]
    chunks = chunk_markdown(doc)
    assert "architecture:loghub_etl#tables" in {c.metadata["parent_id"] for c in chunks}
    assert max(count_tokens(c.page_content) for c in chunks) <= CHUNK_TOKEN_BUDGET


def test_runbooks_name_the_error_codes_they_are_the_reference_for():
    docs = {d.metadata["parent_id"]: d for d in MarkdownDocumentLoader(KNOWLEDGE_DIR / "runbooks", "runbook").load()}
    assert docs["runbook:resource-exhaustion"].metadata["error_class"] == ["DRIVER_OUT_OF_MEMORY"]
    assert docs["runbook:data-quality-breach"].metadata["error_class"] == ["DATA_QUALITY_FAILED"]
    assert "error_class" not in docs["runbook:access-and-permissions"].metadata  # its codes come from the catalog
    chunk = chunk_markdown(docs["runbook:resource-exhaustion"])[0]
    assert chunk.metadata["error_class"] == ["DRIVER_OUT_OF_MEMORY"]  # every chunk, so the exact route finds it


def test_errors_without_a_bracketed_code_get_the_code_of_the_runbook_written_for_them():
    from pipeline_incident_copilot.rag.retrieval.routes.error_code_route import error_codes_in, known_error_codes

    assert known_error_codes("Fatal error: The Python kernel is unresponsive.") == ["DRIVER_OUT_OF_MEMORY"]
    assert known_error_codes("Execution ran out of memory.") == ["DRIVER_OUT_OF_MEMORY"]
    assert known_error_codes("ERROR: DATA_QUALITY_FAILED: min_row_count expected >= 5000, got 2000") == [
        "DATA_QUALITY_FAILED"
    ]  # Jenkins style, no brackets
    assert known_error_codes("[UNAUTHORIZED_ACCESS] Unauthorized access") == ["UNAUTHORIZED_ACCESS"]  # unchanged
    assert known_error_codes("something nobody documented") == []
    assert error_codes_in("Fatal error: The Python kernel is unresponsive.") == []  # memory patterns unchanged
