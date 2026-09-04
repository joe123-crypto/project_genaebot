import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promo_broadcast as pb  # noqa: E402
import json_store  # noqa: E402


def _image(tmp_path):
    p = tmp_path / "ad.png"
    p.write_bytes(b"img")
    return str(p)


def _targets(tmp_path, groups, weekly=None):
    data = {"groups": groups}
    if weekly is not None:
        data["weekly_groups"] = weekly
    p = tmp_path / "targets.json"
    p.write_text(json.dumps(data))
    return str(p)


def _record_sender(sink, ok, out):
    def _send(target, text, image_path):
        sink.append(target)
        return (ok, out)
    return _send


def test_gateway_env_sets_token_and_drops_home(monkeypatch):
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OPENCLAW_HOME", "/should/be/dropped")
    env = pb.gateway_env()
    assert env["OPENCLAW_GATEWAY_TOKEN"] == "tok"
    assert "OPENCLAW_HOME" not in env


def test_broadcast_sends_all_then_is_idempotent_same_day(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(pb, "send_ad", _record_sender(sent, True, '{"messageId":"m"}'))
    targets = _targets(tmp_path, ["1@g.us", "2@g.us"])
    state = str(tmp_path / "state.json")

    rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                      state_file=state, send_delay=0, argv=[])
    assert rc == 0
    assert sent == ["1@g.us", "2@g.us"]
    assert set(json_store.load_json(state, {})["sent"]) == {"1@g.us", "2@g.us"}

    sent.clear()
    rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                      state_file=state, send_delay=0, argv=[])
    assert rc == 0
    assert sent == []  # every group already sent today


def test_permission_error_is_skipped_not_failed_and_not_marked_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "send_ad", _record_sender([], False, "forbidden: only admins can send"))
    targets = _targets(tmp_path, ["1@g.us"])
    state = str(tmp_path / "state.json")
    rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                      state_file=state, send_delay=0, argv=[])
    assert rc == 0
    assert json_store.load_json(state, {})["sent"] == {}


def test_hard_send_failure_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "send_ad", _record_sender([], False, "temporary network error"))
    targets = _targets(tmp_path, ["1@g.us"])
    state = str(tmp_path / "state.json")
    rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                      state_file=state, send_delay=0, argv=[])
    assert rc == 1


def test_missing_image_returns_nonzero(tmp_path):
    targets = _targets(tmp_path, ["1@g.us"])
    rc = pb.broadcast(text="hi", image=str(tmp_path / "absent.png"), targets_file=targets,
                      state_file=str(tmp_path / "state.json"), send_delay=0, argv=[])
    assert rc == 1


def test_weekly_group_excluded_off_its_configured_day(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(pb, "send_ad", _record_sender(sent, True, '{"messageId":"m"}'))
    not_today = (date.today().weekday() + 1) % 7
    targets = _targets(tmp_path, ["daily@g.us"], weekly={"weekly@g.us": [not_today]})
    rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                      state_file=str(tmp_path / "state.json"), send_delay=0, argv=[])
    assert rc == 0
    assert sent == ["daily@g.us"]  # weekly-limited group is not eligible today


def test_resolve_image_rotation_and_fixed():
    a, b = "/x/a.png", "/x/b.png"
    path, rotating = pb._resolve_image([a, b])
    assert rotating is True
    assert path == [a, b][date.today().toordinal() % 2]

    path, rotating = pb._resolve_image("/x/fixed.png")
    assert rotating is False
    assert path == "/x/fixed.png"
