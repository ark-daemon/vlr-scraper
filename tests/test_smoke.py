"""Smoke tests for packaging and schema packaging."""

from pathlib import Path

from vlr_scraper import __version__
from vlr_scraper.config import settings


def test_version():
    assert __version__


def test_settings_base_url():
    assert "vlr.gg" in settings.BASE_URL


def test_schema_ships_with_package():
    schema = Path(__file__).resolve().parents[1] / "vlr_scraper" / "schema.sql"
    assert schema.exists()
    assert "CREATE TABLE" in schema.read_text(encoding="utf-8").upper()
