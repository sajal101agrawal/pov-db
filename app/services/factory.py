from __future__ import annotations

from app.core.config import Settings
from app.sources.bhavcopy import BhavcopySource
from app.sources.nse import NSEArchiveClient
from app.sources.samco import SamcoBhavcopyClient


def build_bhavcopy_source(settings: Settings) -> BhavcopySource:
    return BhavcopySource(
        NSEArchiveClient(
            settings.nse_request_delay_seconds,
            settings.source_retry_attempts,
            settings.source_retry_base_delay_seconds,
            settings.source_retry_max_delay_seconds,
        ),
        SamcoBhavcopyClient(
            settings.source_retry_attempts,
            settings.source_retry_base_delay_seconds,
            settings.source_retry_max_delay_seconds,
        ),
    )
