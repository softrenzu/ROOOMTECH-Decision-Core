from pathlib import Path

import pytest

from app.shared_object_store import LocalObjectStore


def test_local_object_store_round_trip_and_digest(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"rtdc-shared-object")
    store = LocalObjectStore(tmp_path / "objects")
    ref = store.put_file(source, "catalogs/a/source.bin")
    assert ref.uri.startswith("file://")
    assert ref.size_bytes == len(b"rtdc-shared-object")
    materialized = store.materialize(ref.uri, ref.sha256)
    assert materialized.read_bytes() == b"rtdc-shared-object"
    assert store.exists(ref.uri) is True


def test_local_object_store_rejects_escape(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    source = tmp_path / "x"
    source.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        store.put_file(source, "../escape")


def test_local_object_store_detects_digest_mismatch(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    source = tmp_path / "x"
    source.write_text("abc", encoding="utf-8")
    ref = store.put_file(source, "x")
    Path(ref.uri.removeprefix("file://")).write_text("changed", encoding="utf-8")
    with pytest.raises(IOError):
        store.materialize(ref.uri, ref.sha256)
