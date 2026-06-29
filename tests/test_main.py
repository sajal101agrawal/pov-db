import asyncio

import app.api.routes as routes
from app.api.routes import _mask_value, _overall_health_status, _provider_roles
from app.core.config import Settings
from app.main import redact_query


def test_redact_query_masks_sensitive_values() -> None:
    query = "request_token=secret-token&symbol=MARUTI&api_key=raw-api-value&empty="

    redacted = redact_query(query)

    assert redacted == "request_token=***&symbol=MARUTI&api_key=***&empty="
    assert "secret-token" not in redacted
    assert "raw-api-value" not in redacted


def test_system_health_masks_config_values() -> None:
    assert _mask_value("abcdefghijkl", keep=3) == "abc...jkl"
    assert _mask_value("short", keep=3) == "***"
    assert _mask_value(None) is None


def test_system_health_overall_status_respects_required_checks() -> None:
    assert _overall_health_status({"database": {"status": "ok"}}) == "ok"
    assert _overall_health_status({"kite": {"status": "fail", "required": False}}) == "warn"
    assert _overall_health_status({"database": {"status": "fail", "required": True}}) == "fail"
    assert _overall_health_status({"live_state": {"status": "warn", "required": True}}) == "warn"


def test_system_health_provider_roles() -> None:
    settings = Settings(
        live_quote_provider="kite",
        live_option_summary_provider="KITE",
        live_option_chain_provider="nse",
    )

    assert _provider_roles(settings, "kite") == ["quote", "option_summary"]
    assert _provider_roles(settings, "nse") == ["option_chain"]
    assert _provider_roles(settings, "dhan") == []


def test_system_health_payload_includes_current_sources(monkeypatch) -> None:
    async def database(repo):
        return {"status": "ok"}

    async def redis(cache_service):
        return {"status": "ok"}

    async def kite(settings, repo, cache_service, symbol):
        return {"status": "ok", "active_for": ["quote", "option_summary"]}

    async def nse(settings, symbol):
        return {"status": "ok", "active_for": ["option_chain"]}

    async def yahoo(repo, settings, symbol):
        return {"status": "ok", "active_for": []}

    async def dhan(settings, cache_service, symbol):
        return {"status": "disabled", "active_for": []}

    async def live_state(settings, repo, cache_service, symbol):
        return {"status": "ok", "quote_provider": "kite", "option_provider": "kite"}

    monkeypatch.setattr(routes, "_check_database", database)
    monkeypatch.setattr(routes, "_check_redis", redis)
    monkeypatch.setattr(routes, "_check_kite", kite)
    monkeypatch.setattr(routes, "_check_nse_option_chain", nse)
    monkeypatch.setattr(routes, "_check_yahoo", yahoo)
    monkeypatch.setattr(routes, "_check_dhan", dhan)
    monkeypatch.setattr(routes, "_check_live_state", live_state)

    settings = Settings(
        live_quote_provider="kite",
        live_option_summary_provider="kite",
        live_option_chain_provider="nse",
    )

    result = asyncio.run(
        routes.system_health(
            symbol="maruti",
            settings=settings,
            repo=object(),  # type: ignore[arg-type]
            cache_service=object(),  # type: ignore[arg-type]
        )
    )

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["symbol"] == "MARUTI"
    assert result["current_sources"] == {
        "live_quote_provider": "kite",
        "live_option_summary_provider": "kite",
        "live_option_chain_provider": "nse",
    }
    assert result["checks"]["kite"]["required"] is True
    assert result["checks"]["yahoo"]["required"] is False
