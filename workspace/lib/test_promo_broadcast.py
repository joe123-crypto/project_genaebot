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


def _sequence_sender(sink, outs):
    """send_ad stub returning the ``outs`` pairs in order; the last repeats."""
    calls = {"n": 0}

    def _send(target, text, image_path):
        sink.append(target)
        pair = outs[min(calls["n"], len(outs) - 1)]
        calls["n"] += 1
        return pair
    return _send


def test_not_dispatched_error_is_not_resent_and_marked_sent(tmp_path, monkeypatch):
    # The WhatsApp link was mid-reconnect (status 428/500/503). The gateway queues
    # the send and replays it on reconnect (live: 2026-10-18 06:24 and 2026-10-19
    # 15:17 UTC, where our own re-send posted the ad to OnPoint ZW Ads twice), so
    # the engine must NOT re-send: one attempt, group recorded as sent, run ok.
    sent = []
    down = (False, "OutboundDeliveryError: No active WhatsApp Web listener (account: default)"
                   " <- PlatformMessageNotDispatchedError")
    up = (True, '{"messageId":"m"}')
    monkeypatch.setattr(pb, "send_ad", _sequence_sender(sent, [down, up]))
    waits = []
    targets = _targets(tmp_path, ["1@g.us", "2@g.us"])
    state = str(tmp_path / "state.json")
    rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                      state_file=state, send_delay=0, argv=[], sleep=waits.append,
                      start_jitter=0, cron_timeout=None)
    assert rc == 0
    assert sent == ["1@g.us", "2@g.us"]  # 1@g.us attempted once only, never re-sent
    assert len(waits) == 1  # just the normal inter-group gap, no retry wait
    assert set(json_store.load_json(state, {})["sent"]) == {"1@g.us", "2@g.us"}


def test_not_dispatched_group_is_not_reposted_on_a_same_day_rerun(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(pb, "send_ad", _record_sender(
        sent, False, "PlatformMessageNotDispatchedError: No active WhatsApp Web listener"))
    targets = _targets(tmp_path, ["1@g.us"])
    state = str(tmp_path / "state.json")
    kw = dict(text="hi", image=_image(tmp_path), targets_file=targets, state_file=state,
              send_delay=0, argv=[], sleep=lambda s: None, start_jitter=0, cron_timeout=None)
    assert pb.broadcast(**kw) == 0
    assert pb.broadcast(**kw) == 0
    assert sent == ["1@g.us"]  # the rerun skips it: the gateway already has it


def test_engine_has_no_resend_retry_knobs():
    # Guard against reintroducing the #124 re-send loop.
    assert not hasattr(pb, "SEND_RETRY_MAX")
    assert not hasattr(pb, "SEND_RETRY_WAIT")


def test_ambiguous_errors_are_not_retried(tmp_path, monkeypatch):
    # A subprocess timeout or an unknown_after_send state might have gone out, so
    # retrying could double-post: these stay single-attempt hard failures.
    for out in ("exec error: timeout after 180s",
                "delivery state is unknown_after_send"):
        sent = []
        monkeypatch.setattr(pb, "send_ad", _record_sender(sent, False, out))
        waits = []
        targets = _targets(tmp_path, ["1@g.us"])
        state = str(tmp_path / "state.json")
        rc = pb.broadcast(text="hi", image=_image(tmp_path), targets_file=targets,
                          state_file=state, send_delay=0, argv=[], sleep=waits.append,
                          start_jitter=0, cron_timeout=None)
        assert rc == 1
        assert sent == ["1@g.us"]  # no retry
        assert waits == []


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
