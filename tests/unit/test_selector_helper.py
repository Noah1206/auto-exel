"""SelectorHelper - YAML 로드 테스트."""
from __future__ import annotations

import pytest

from src.core.selector_helper import SelectorHelper
from src.exceptions import ConfigError


def test_load_defaults():
    helper = SelectorHelper("config/selectors.yaml")
    assert helper.get("product_page.price")
    assert len(helper.get("order_page.recipient_name")) >= 2


def test_missing_path():
    helper = SelectorHelper("config/selectors.yaml")
    with pytest.raises(ConfigError):
        helper.get("nonexistent.path")


def test_missing_file():
    with pytest.raises(ConfigError):
        SelectorHelper("config/does_not_exist.yaml")
