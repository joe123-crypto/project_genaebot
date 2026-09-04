import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json_store  # noqa: E402


def test_load_json_missing_returns_default(tmp_path):
    assert json_store.load_json(str(tmp_path / "nope.json"), {"a": 1}) == {"a": 1}


def test_load_json_corrupt_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert json_store.load_json(str(p), {}) == {}


def test_load_json_valid_roundtrips(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"x": [1, 2]}))
    assert json_store.load_json(str(p), {}) == {"x": [1, 2]}


def test_write_json_atomic_creates_parent_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "sub" / "dir" / "state.json"
    json_store.write_json_atomic(str(target), {"k": "v"})
    assert json.loads(target.read_text()) == {"k": "v"}
    assert not (tmp_path / "sub" / "dir" / "state.json.tmp").exists()


def test_write_json_atomic_overwrites(tmp_path):
    target = str(tmp_path / "s.json")
    json_store.write_json_atomic(target, {"first": True})
    json_store.write_json_atomic(target, {"second": True})
    assert json_store.load_json(target, {}) == {"second": True}
