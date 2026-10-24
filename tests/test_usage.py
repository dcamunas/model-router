import io
import json
from datetime import UTC, datetime, timedelta

from model_router import usage
from model_router.catalog import Usage
from model_router.cli import main
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


def test_the_status_line_reads_context_and_windows():
    status = usage.read_status(_status_json())
    assert (status.context, status.window_size, status.five_hour, status.seven_day) == (412_005, 1_000_000, 22.4, 61.2)
    assert usage.statusline_text(status).startswith("ctx 412k/1M · 5h 22% · week 61% (resets ")


def test_the_status_line_shows_what_it_has_and_nothing_before_the_first_call():
    assert usage.statusline_text(usage.read_status({})) == ""
    data = _status_json()
    del data["rate_limits"]
    assert usage.statusline_text(usage.read_status(data)) == "ctx 412k/1M"
    # right after /compact current_usage is null while the totals still hold the old context: show none
    data = {"context_window": {"total_input_tokens": 900_000, "current_usage": None}, "rate_limits": {"seven_day": {}}}
    assert usage.statusline_text(usage.read_status(data)) == ""


def test_odd_numbers_never_break_the_status_line(state, monkeypatch, capsys):
    data = _status_json(week_resets=1_793_300_000_000)  # milliseconds
    assert "(resets " in usage.statusline_text(usage.read_status(data))
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


def test_config_sets_the_profiles_usage_reads(state, tmp_path, capsys):
    assert main(["config", "--usage-profiles", str(tmp_path)]) == 0
    assert str(tmp_path.name) in capsys.readouterr().out
    assert main(["config", "--usage-profiles", "none"]) == 0


# ---- what the transcripts spent -------------------------------------------------------------------------------


def test_spent_reads_main_and_subagent_calls_each_message_once_in_the_period(tmp_path):
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
    assert sorted(usage.MODELS[c.model_id].short for c in spent.calls) == ["fable", "haiku", "opus"]
    assert spent.calls[-1].usage.cache_read_tokens == 450_000  # the latest last


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
                          Usage(0, 1000 * (1 + i % 3), 100_000 * (1 + (i * 7) % 5), 0))
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


def test_usage_counts_the_calls_and_asks_for_samples(tmp_path, capsys, freeze_clock):
    freeze_clock(T0 + timedelta(hours=1))
    _write(tmp_path / "projects" / "-r" / "s.jsonl", _assistant("m1", at=T0, cache_read=450_000))
    assert main(["usage", "--profile", str(tmp_path), "--days", "2"]) == 0
    out = capsys.readouterr().out
    assert "1 calls; seven-day window" in out and "cache read" not in out and "not enough samples yet" in out


# ---- Fable's own weekly window --------------------------------------------------------------------------------


def _fable_probe(used=0.01, resets="2026-10-31T13:00:00+00:00"):
    from model_router.limits import Probe

    return Probe("allowed", windows={"five_hour": {"utilization": 0.0, "resets_at": "2026-10-24T19:30:00+00:00"},
                                     "seven_day": {"utilization": 0.02, "resets_at": resets},
                                     usage.FABLE_WINDOW: {"utilization": used, "resets_at": resets}})


def test_fable_shows_apart_with_one_reset_when_both_weeks_share_it():
    status = usage.read_status(_status_json(week=2.0, week_resets=1_793_451_600))
    status.fable, status.fable_resets = 1.0, 1_793_451_600
    text = usage.statusline_text(status)
    assert text.endswith("· week 2% · fable 1% (resets " + text.rsplit("(resets ", 1)[1])
    assert text.count("resets") == 1
    status.fable_resets = 1_793_000_000
    assert usage.statusline_text(status).count("resets") == 2


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


def test_a_fable_reading_without_a_reset_keeps_the_weeks_reset():
    status = usage.read_status(_status_json(week=2.0, week_resets=1_793_451_600))
    status.fable, status.fable_resets = 1.0, None
    text = usage.statusline_text(status)
    assert "week 2% (resets " in text and text.endswith("fable 1%")


def test_an_account_wide_limit_hides_fables_reading(state, freeze_clock):
    from model_router import limits

    freeze_clock(T0)
    limits.record_check("fable", _fable_probe())
    limits.record_check("haiku", limits.Probe("rejected", until=T0 + timedelta(hours=3), window="five_hour"))
    assert usage.fable_window(T0) is None


def test_the_statusline_command_shows_fable_and_starts_one_probe(state, freeze_clock, monkeypatch, capsys,
                                                                  _no_fable_probe):
    from model_router import limits

    freeze_clock(datetime.now(UTC))
    limits.record_check("fable", _fable_probe())
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_status_json(week=2.0))))
        assert main(["statusline"]) == 0
    assert "fable 1%" in capsys.readouterr().out
    assert _no_fable_probe == [1]  # throttled: one probe per refresh


def test_logging_off_still_reads_fables_window(state, monkeypatch, _no_fable_probe):
    monkeypatch.setenv("MODEL_ROUTER_HOOK_LOG", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_status_json())))
    assert main(["statusline"]) == 0
    assert _no_fable_probe == [1] and not (state / usage.USAGE_LOG).exists()
