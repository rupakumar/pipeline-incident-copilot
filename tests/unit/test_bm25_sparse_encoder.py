from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import doc_vector, query_vector, tokenize


def test_error_codes_kept_whole_and_split():
    tokens = tokenize("[DELTA_METADATA_MISMATCH] mismatch")
    assert "delta_metadata_mismatch" in tokens
    assert {"delta", "metadata", "mismatch"} <= set(tokens)


def test_query_terms_counted_once():
    indices, values = query_vector("path path path missing")
    assert len(indices) == len(set(indices)) and set(values) == {1.0}


def test_doc_vector_saturates_term_frequency():
    _, once = doc_vector(["error"], avgdl=1.0)
    _, many = doc_vector(["error"] * 50, avgdl=1.0)
    assert many[0] < 2.3 and many[0] > once[0]  # bounded by k1 + 1
