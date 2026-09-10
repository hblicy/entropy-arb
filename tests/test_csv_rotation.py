import gzip
from datetime import date

from entropy_arb.csv_rotation import rotate_csv_gzip


def test_rotate_csv_gzip_uses_source_stem_and_utc_day(tmp_path):
    path = tmp_path / "signals.csv"
    path.write_text("header\nrow\n", encoding="utf-8")

    result = rotate_csv_gzip(str(path), date(2026, 9, 10))

    expected = tmp_path / "signals-20260910.csv.gz"
    assert result.archive_path == str(expected)
    assert result.compressed is True
    assert not path.exists()
    with gzip.open(expected, "rt", encoding="utf-8") as fh:
        assert fh.read() == "header\nrow\n"


def test_rotate_csv_gzip_supports_custom_name(tmp_path):
    path = tmp_path / "anth.csv"
    path.write_text("data", encoding="utf-8")

    result = rotate_csv_gzip(str(path), date(2026, 9, 10))

    assert result.archive_path == str(tmp_path / "anth-20260910.csv.gz")


def test_rotate_csv_gzip_preserves_conflicts(tmp_path):
    path = tmp_path / "signals.csv"
    existing = tmp_path / "signals-20260910.csv.gz"
    existing.write_bytes(b"existing")
    path.write_text("new", encoding="utf-8")

    result = rotate_csv_gzip(str(path), date(2026, 9, 10))

    assert existing.read_bytes() == b"existing"
    assert result.archive_path == str(
        tmp_path / "signals-20260910.csv.gz.1")
    assert result.compressed is True


def test_rotate_csv_gzip_keeps_raw_archive_when_compression_fails(
        tmp_path, monkeypatch):
    path = tmp_path / "signals.csv"
    path.write_text("important", encoding="utf-8")

    def fail_gzip_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(gzip, "open", fail_gzip_open)

    result = rotate_csv_gzip(str(path), date(2026, 9, 10))

    raw = tmp_path / "signals-20260910.csv"
    assert result.archive_path == str(raw)
    assert result.compressed is False
    assert raw.read_text(encoding="utf-8") == "important"
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp.gz"))


def test_rotate_csv_gzip_keeps_raw_archive_when_validation_is_truncated(
        tmp_path, monkeypatch):
    path = tmp_path / "signals.csv"
    path.write_text("important", encoding="utf-8")
    original_gzip_open = gzip.open

    def fail_validation(path, mode="rb", *args, **kwargs):
        if mode == "rb":
            raise EOFError("truncated gzip")
        return original_gzip_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(gzip, "open", fail_validation)

    result = rotate_csv_gzip(str(path), date(2026, 9, 10))

    raw = tmp_path / "signals-20260910.csv"
    assert result.archive_path == str(raw)
    assert result.compressed is False
    assert raw.read_text(encoding="utf-8") == "important"
    assert not list(tmp_path.glob("*.tmp.gz"))
