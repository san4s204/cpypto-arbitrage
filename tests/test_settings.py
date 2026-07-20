from pathlib import Path

from app.config.settings import AppSettings


def test_default_exchange_fees_cover_the_default_exchange_set() -> None:
    settings = AppSettings(project_root=Path.cwd())

    assert set(settings.exchanges) <= set(settings.paper_fee_bps)
    assert settings.paper_fee_bps["bitget"] == 10
