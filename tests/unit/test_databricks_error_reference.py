from pipeline_incident_copilot.rag.ingestion.loaders.databricks_error_reference_loader import parse_error_reference

PAGE = """
<h2>Error classes</h2>
<h3 id=uc_volume_not_found>UC_VOLUME_NOT_FOUND<a href=#x>​</a></h3>
<p><a href=/sqlstates>SQLSTATE: 42704</a></p>
<p>Volume <code>&lt;name></code> does not exist. Please use '<code>SHOW VOLUMES</code>' to list available volumes.</p>
<h3 id=unauthorized_access>UNAUTHORIZED_ACCESS</h3>
<p>SQLSTATE: 42501</p>
<p>Unauthorized access:</p>
<p><code>&lt;report></code></p>
<h2>Not an error section</h2>
<p>ignored text</p>
"""


def test_parses_name_sqlstate_and_message():
    catalog = parse_error_reference(PAGE)
    assert catalog["UC_VOLUME_NOT_FOUND"] == {
        "sqlState": "42704",
        "message": "Volume <name> does not exist. Please use 'SHOW VOLUMES' to list available volumes.",
    }
    assert catalog["UNAUTHORIZED_ACCESS"]["message"] == "Unauthorized access: <report>"


def test_text_outside_error_entries_is_ignored():
    assert all("ignored" not in v["message"] for v in parse_error_reference(PAGE).values())
