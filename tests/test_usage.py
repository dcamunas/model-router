import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from model_router import usage
from model_router.catalog import Usage
from model_router.cli import _token_count, main
from model_router.hook import handle
from model_router.paths import write_config

T0 = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def _status_json(*, context=410_000, five=22.4, week=61.2, week_resets=1_793_300_000, session="s1"):
    return {"session_id": session, "model": {"id": "claude-opus-5-5"},
            "context_window": {"context_window_size": 1_000_000,
                               "current_usage": {"input_tokens": 5, "cache_read_input_tokens": context,
                                                 "cache_creation_input_tokens": 2000, "output_tokens": 300}},
            "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": 1_793_000_000},
                            "seven_day": {"used_percentage": week, "resets_at": week_resets}}}


def _assistant(msg_id, *, at, cache_read=1000, cache_write=100, output=10, model="claude-opus-5-5", side=False):
    usage_ = {"input_tokens": 2, "output_tokens": output, "cache_read_input_tokens": cache_read,
              "cache_creation_input_tokens": cache_write}
    entry = {"timestamp": at.isoformat().replace("+00:00", "Z"), "isSidechain": side,
             "message": {"role": "assistant", "id": msg_id, "model": model, "usage": usage_,
                         "content": [{"type": "text", "text": "secret words"}]}}
    return [entry, entry]  # a transcript repeats a message's usage on every content block


def _write(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return path


# ---- status line ----------------------------------------------------------------------------------------------


def test_the_status_line_reads_context_and_windows_and_marks_a_long_context():
    status = usage.read_status(_status_json())
    assert (status.context, status.window_size, status.five_hour, status.seven_day) == (412_005, 1_000_000, 22.4, 61.2)
    text = usage.statusline_text(status, warn_at=300_000)
    assert text.startswith("⚠ ctx 412k/1M · /compact? · 5h 22% · week 61% (resets ")
    assert "⚠" not in usage.statusline_text(status, warn_at=500_000)
    assert "⚠" not in usage.statusline_text(status, warn_at=0)


def test_the_status_line_shows_what_it_has_and_nothing_before_the_first_call():
    assert usage.statusline_text(usage.read_status({}), warn_at=300_000) == ""
    data = _status_json()
    del data["rate_limits"]
    assert usage.statusline_text(usage.read_status(data), warn_at=300_000) == "⚠ ctx 412k/1M · /compact?"
    # right after /compact current_usage is null while the totals still hold the old context: show none
    data = {"context_window": {"total_input_tokens": 900_000, "current_usage": None}, "rate_limits": {"seven_day": {}}}
    assert usage.statusline_text(usage.read_status(data), warn_at=300_000) == ""


def test_odd_numbers_never_break_the_status_line(state, monkeypatch, capsys):
    data = _status_json(week_resets=1_793_300_000_000)  # milliseconds
    assert "(resets " in usage.statusline_text(usage.read_status(data), warn_at=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_status_json()).replace("61.2", "Infinity")))
    assert main(["statusline"]) == 0
    out = capsys.readouterr().out
    assert "week" not in out and "5h 22%" in out


def test_a_sample_is_kept_when_something_moved_and_never_twice_for_the_same_numbers(state):
    status = usage.read_status(_status_json())
    assert usage.keep_sample(status, now=1000.0)
    assert not usage.keep_sample(status, now=1001.0)
    assert usage.keep_sample(usage.read_status(_status_json(week=61.5)), now=1002.0)  # the window moved
    small = usage.read_status(_status_json(week=61.5, context=412_000))  # the context moved by under CONTEXT_STEP
    assert not usage.keep_sample(small, now=1003.0)
    assert usage.keep_sample(small, now=1003.0 + usage.SAMPLE_SECONDS)
    rows = [json.loads(line) for line in (state / usage.USAGE_LOG).read_text().splitlines()]
    assert len(rows) == 3 and rows[1]["seven_day"] == 61.5
    assert "secret" not in (state / usage.USAGE_LOG).read_text()


def test_a_status_with_nothing_in_it_is_never_kept(state):
    assert not usage.keep_sample(usage.read_status({"session_id": "s"}), now=1.0)
    assert not (state / usage.USAGE_LOG).exists()


def test_the_statusline_command_never_fails_and_keeps_a_sample(state, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_status_json())))
    assert main(["statusline"]) == 0
    assert "week 61%" in capsys.readouterr().out
    assert (state / usage.USAGE_LOG).exists()
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main(["statusline"]) == 0
    assert capsys.readouterr().out == ""


def test_a_child_session_or_logging_off_keeps_no_sample(state, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOOK_LOG", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_status_json())))
    assert main(["statusline"]) == 0
    assert not (state / usage.USAGE_LOG).exists()


# ---- context warning ------------------------------------------------------------------------------------------


def test_last_context_is_the_main_conversations_last_call(tmp_path):
    path = _write(tmp_path / "t.jsonl", [
        *_assistant("m1", at=T0, cache_read=100_000),
        *_assistant("m2", at=T0 + timedelta(seconds=5), cache_read=350_000),
        *_assistant("sub", at=T0 + timedelta(seconds=9), cache_read=5_000, side=True),
    ])
    assert usage.last_context(path) == 350_102
    assert usage.last_context(tmp_path / "missing.jsonl") is None


def test_the_warning_comes_once_per_band(state, tmp_path):
    path = tmp_path / "t.jsonl"
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(path)}
    _write(path, _assistant("m1", at=T0, cache_read=250_000))
    assert handle(event) is None
    _write(path, _assistant("m2", at=T0, cache_read=320_000))
    out = handle(event)
    assert out is not None and "320k tokens" in out["systemMessage"]
    assert handle(event) is None  # same band, already warned
    _write(path, _assistant("m3", at=T0, cache_read=520_000))
    assert handle(event) is not None
    assert handle({**event, "session_id": "s2"}) is not None  # another session is warned on its own
    _write(path, _assistant("m4", at=T0, cache_read=60_000))  # a /compact
    assert handle(event) is None
    _write(path, _assistant("m5", at=T0, cache_read=310_000))
    assert handle(event) is not None  # grown again: warned again


def test_the_warning_is_off_at_zero_and_silent_in_a_child_session(state, tmp_path, monkeypatch):
    path = _write(tmp_path / "t.jsonl", _assistant("m1", at=T0, cache_read=800_000))
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(path)}
    monkeypatch.setenv("MODEL_ROUTER_CHILD", "1")
    assert handle(event) is None
    monkeypatch.delenv("MODEL_ROUTER_CHILD")
    write_config(context_warn=0)
    assert usage.warn_threshold() == 0 and usage.bands(0) == ()
    assert handle(event) is None


def test_token_counts_read_k_and_m():
    assert (_token_count("300k"), _token_count("1.2M"), _token_count("250000"), _token_count("0")) == (
        300_000, 1_200_000, 250_000, 0)
    with pytest.raises(ValueError):
        _token_count("lots")
    with pytest.raises(ValueError):
        _token_count("-5k")
    with pytest.raises(ValueError):
        _token_count("inf")


def test_config_sets_the_warning_and_the_profiles(state, tmp_path, capsys):
    assert main(["config", "--context-warn", "400k", "--usage-profiles", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "context warning from: 400k tokens" in out and str(tmp_path.name) in out
    assert usage.warn_threshold() == 400_000
    assert main(["config", "--usage-profiles", "none"]) == 0


# ---- what the transcripts spent -------------------------------------------------------------------------------


def test_spent_splits_main_and_subagents_counts_each_message_once_and_bands_the_main_calls(tmp_path):
    profile = tmp_path / "profile"
    project = profile / "projects" / "-repo"
    _write(project / "session.jsonl", [
        *_assistant("m1", at=T0, cache_read=150_000),
        *_assistant("m2", at=T0 + timedelta(minutes=1), cache_read=450_000, model="claude-fable-5-1[1m]"),
        *_assistant("m3", at=T0 - timedelta(days=30), cache_read=999),  # before the period
        *_assistant("x", at=T0, model="gpt-6-luna"),  # not a catalogued model
    ])
    _write(project / "session" / "subagents" / "agent-a1.jsonl", _assistant("s1", at=T0, cache_read=20_000,
                                                                            model="claude-haiku-5-5"))
    spent = usage.spent([profile], since=T0 - timedelta(days=1))
    assert spent.parts["main"]["opus"].calls == 1 and spent.parts["main"]["fable"].cache_read_tokens == 450_000
    assert spent.parts["subagents"]["haiku"].calls == 1
    assert spent.bands[100_000].calls == 1 and spent.bands[400_000].calls == 1
    assert spent.unknown_models == 1 and len(spent.calls) == 3


def test_a_message_copied_into_a_resumed_session_is_counted_once(tmp_path):
    profile = tmp_path / "profile"
    _write(profile / "projects" / "-r" / "first.jsonl", _assistant("m1", at=T0))
    _write(profile / "projects" / "-r" / "resumed.jsonl", [*_assistant("m1", at=T0), *_assistant("m2", at=T0)])
    assert len(usage.spent([profile], since=T0 - timedelta(days=1)).calls) == 2


def test_the_usage_log_keeps_its_newer_half_past_the_limit(state, monkeypatch):
    monkeypatch.setattr(usage, "MAX_LOG_BYTES", 2000)
    for i in range(40):
        usage.keep_sample(usage.read_status(_status_json(week=float(i))), now=1000.0 + i)
    text = (state / usage.USAGE_LOG).read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    assert len(text) <= 2000 and rows[-1]["seven_day"] == 39.0 and rows[0]["seven_day"] > 0


def test_model_names_with_suffixes_resolve_to_the_catalogue():
    assert usage.model_id("claude-opus-5-5") == "claude-opus-5-5"
    assert usage.model_id("claude-fable-5-1[1m]") == "claude-fable-5-1"
    assert usage.model_id("gpt-6-luna") is None


# ---- the fit --------------------------------------------------------------------------------------------------


def _sample(at, week, resets=1_793_300_000):
    return {"at": at.isoformat(), "seven_day": week, "seven_day_resets": resets}


def test_intervals_run_from_one_rise_to_the_next_skip_stale_samples_and_break_on_gaps_and_resets():
    m = lambda minutes: T0 + timedelta(minutes=minutes)  # noqa: E731
    rows = [_sample(m(0), 10), _sample(m(5), 10), _sample(m(10), 11),  # first rise: only an anchor
            _sample(m(15), 10),  # an idle session's older percent: skipped, the chain holds
            _sample(m(20), 11), _sample(m(30), 12),  # 10 -> 30: one point
            _sample(m(400), 13),  # a gap too long: the chain breaks
            _sample(m(410), 14), _sample(m(420), 14), _sample(m(430), 16),  # 410 -> 430: two points
            _sample(m(440), 1, resets=1_793_900_000), _sample(m(450), 2, resets=1_793_900_000),  # a new week
            _sample(m(460), 3, resets=1_793_900_000)]
    spans = usage.intervals(rows)
    assert [(s.start, s.end, s.used) for s in spans] == [
        (m(10).timestamp(), m(30).timestamp(), 1), (m(410).timestamp(), m(430).timestamp(), 2),
        (m(450).timestamp(), m(460).timestamp(), 1)]
    assert len(usage.intervals(rows, since=m(400).timestamp())) == 2  # only samples from `since` on


def test_the_fit_finds_the_weighting_the_window_follows():
    calls, rows, week = [], [], 0.0
    for i in range(30):
        start = T0 + timedelta(minutes=10 * i)
        rows.append(_sample(start, round(week, 6)))
        # cache reads swing a lot from one interval to the next, output barely: a window that follows list price
        # tells the weightings apart
        call = usage.Call((start + timedelta(minutes=5)).timestamp(), "claude-opus-5-5",
                          Usage(0, 1000 * (1 + i % 3), 100_000 * (1 + (i * 7) % 5), 0), subagent=False)
        calls.append(call)
        week += 0.5 * usage.cost_usd(call.model_id, call.usage)
    rows.append(_sample(T0 + timedelta(minutes=300), round(week, 6)))
    fits = usage.fit(calls, usage.intervals(rows))
    assert fits[0].name == "list price" and fits[0].spread < 0.01 and abs(fits[0].per_point - 2.0) < 0.01
    assert all(f.spread > 0.05 for f in fits[1:])
    assert usage.fit(calls, usage.intervals(rows[:10])) == []  # too few intervals


def test_usage_reports_an_empty_profile_without_failing(tmp_path, capsys):
    assert main(["usage", "--profile", str(tmp_path)]) == 0
    assert "no calls found" in capsys.readouterr().out


def test_usage_prints_the_tables_and_asks_for_samples(tmp_path, capsys, freeze_clock):
    freeze_clock(T0 + timedelta(hours=1))
    _write(tmp_path / "projects" / "-r" / "s.jsonl", _assistant("m1", at=T0, cache_read=450_000))
    assert main(["usage", "--profile", str(tmp_path), "--days", "2"]) == 0
    out = capsys.readouterr().out
    assert "cache read" in out and "400k-700k" in out and "not enough samples yet" in out


# ---- Fable's own weekly window --------------------------------------------------------------------------------


def _fable_probe(used=0.01, resets="2026-10-31T13:00:00+00:00"):
    from model_router.limits import Probe

    return Probe("allowed", windows={"five_hour": {"utilization": 0.0, "resets_at": "2026-10-24T19:30:00+00:00"},
                                     "seven_day": {"utilization": 0.02, "resets_at": resets},
                                     usage.FABLE_WINDOW: {"utilization": used, "resets_at": resets}})


def test_fable_shows_apart_with_one_reset_when_both_weeks_share_it():
    status = usage.read_status(_status_json(week=2.0, week_resets=1_793_451_600))
    status.fable, status.fable_resets = 1.0, 1_793_451_600
    text = usage.statusline_text(status, warn_at=0)
    assert text.endswith("· week 2% · fable 1% (resets " + text.rsplit("(resets ", 1)[1])
    assert text.count("resets") == 1
    status.fable_resets = 1_793_000_000
    assert usage.statusline_text(status, warn_at=0).count("resets") == 2


def test_fable_window_comes_from_the_last_fable_probe_while_fresh(state, freeze_clock):
    from model_router import limits

    freeze_clock(T0)
    limits.record_check("fable", _fable_probe())
    assert usage.fable_window(T0 + timedelta(hours=1)) == (1.0, int(datetime(2026, 10, 31, 13, tzinfo=UTC).timestamp()))
    limits.record_check("haiku", limits.Probe("allowed", windows={"seven_day": {"utilization": 0.03}}))
    assert usage.fable_window(T0 + timedelta(hours=1)) is not None  # a Haiku probe does not wipe Fable's window
    assert usage.fable_window(T0 + timedelta(hours=7)) is None  # too old to show


def test_fable_is_not_shown_or_probed_without_fable(state, freeze_clock):
    from model_router import limits

    freeze_clock(T0)
    limits.record_check("fable", _fable_probe())
    write_config(models=["haiku", "sonnet", "opus"])
    assert usage.fable_window(T0) is None
    assert not usage.refresh_fable(now=1000.0, start=lambda: None)


def test_a_limited_fable_shows_full_until_its_reset(state, freeze_clock):
    from model_router import limits

    freeze_clock(T0)
    until = T0 + timedelta(days=2)
    limits.record_check("fable", limits.Probe("rejected", until=until, window=usage.FABLE_WINDOW))
    assert usage.fable_window(T0) == (100.0, int(until.timestamp()))


def test_the_fable_probe_starts_at_most_once_per_refresh(state):
    started = []
    assert usage.refresh_fable(now=10_000.0, start=lambda: started.append(1))
    assert not usage.refresh_fable(now=10_000.0 + usage.FABLE_REFRESH_SECONDS - 1, start=lambda: started.append(1))
    assert usage.refresh_fable(now=10_000.0 + usage.FABLE_REFRESH_SECONDS, start=lambda: started.append(1))
    assert started == [1, 1]
