from app.main import redact_query


def test_redact_query_masks_sensitive_values() -> None:
    query = "request_token=secret-token&symbol=MARUTI&api_key=raw-api-value&empty="

    redacted = redact_query(query)

    assert redacted == "request_token=***&symbol=MARUTI&api_key=***&empty="
    assert "secret-token" not in redacted
    assert "raw-api-value" not in redacted
