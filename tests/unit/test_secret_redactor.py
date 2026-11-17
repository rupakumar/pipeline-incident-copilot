from pipeline_incident_copilot.guardrails.input.secret_redactor import redact

FAKE_DATABRICKS_TOKEN = "dapi" + "0123456789abcdef" * 2  # built at runtime so secret scanners don't flag the source


def test_masks_known_secret_formats():
    text = f"token {FAKE_DATABRICKS_TOKEN} and sk-ant-abcdefghijk123 and Bearer abcdefghijklmnopqrstu"
    masked = redact(text)
    assert "0123456789abcdef0123" not in masked
    assert "sk-ant-***" in masked and "Bearer ***" in masked


def test_leaves_normal_text_alone():
    text = "[PATH_NOT_FOUND] Path does not exist: dbfs:/Volumes/workspace/raw_logs/a.log"
    assert redact(text) == text


def test_grafana_service_account_tokens_are_masked():
    from pipeline_incident_copilot.guardrails.input.secret_redactor import redact

    assert "glsa_abcdefghijklmnopqrstuvwx" not in redact("auth glsa_abcdefghijklmnopqrstuvwx_12345678")
