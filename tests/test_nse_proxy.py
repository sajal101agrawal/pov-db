from __future__ import annotations

from app.core.config import Settings
from app.services.factory import build_bhavcopy_source, build_corporate_actions_source


def test_blank_nse_proxy_url_is_treated_as_unconfigured() -> None:
    assert Settings(nse_proxy_url="").nse_proxy_url is None


def test_nse_proxy_url_is_passed_to_nse_sources() -> None:
    settings = Settings(nse_proxy_url="http://user:pass@example.test:7000")

    bhavcopy = build_bhavcopy_source(settings)
    corporate_actions = build_corporate_actions_source(settings)

    assert bhavcopy.nse.proxy_url == settings.nse_proxy_url
    assert corporate_actions.proxy_url == settings.nse_proxy_url
    assert bhavcopy.samco.retry_attempts == settings.source_retry_attempts
