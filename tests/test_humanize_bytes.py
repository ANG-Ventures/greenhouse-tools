from tools.humanize_bytes.humanize_bytes import humanize_bytes
import pytest


def test_bytes():
    assert humanize_bytes(0) == "0 B"
    assert humanize_bytes(512) == "512 B"


def test_kib():
    assert humanize_bytes(1024) == "1.0 KiB"
    assert humanize_bytes(1536) == "1.5 KiB"


def test_mib():
    assert humanize_bytes(1048576) == "1.0 MiB"


def test_negative_raises():
    with pytest.raises(ValueError):
        humanize_bytes(-1)
