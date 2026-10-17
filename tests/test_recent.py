"""Session discovery (recent.py): read-only, bounded, tolerant of format changes."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher import recent
from tests.conftest import (
    SID, rec_ai, rec_assistant, rec_custom, rec_user, standard_sessions, ts, write_session,
)


def names(res):
    return [(s.id, s.name, s.source) for s in res.sessions]


def test_project_key_follows_claudes_rule(fake_home):
    key = recent.project_key("/Users/me/repo/.claude/worktrees/Feature-X")
    assert key == "-Users-me-repo--claude-worktrees-Feature-X"
    assert recent.project_key("/tmp/a b/ñ_x") == "-tmp-a-b---x"
    assert recent.project_key("/tmp/🙂x") == "-tmp---x"  # one dash per UTF-16 unit, like JavaScript
    long = "/" + "a" * 250
    key = recent.project_key(long)
    # cut to 200 chars, then "-" and the base-36 of |Java-style hash| of the path (as Claude does)
    assert key[:200] == "-" + "a" * 199 and key[200] == "-"
    assert key[201:] == recent._base36(abs(recent._java_hash(long)))
    assert recent._java_hash("hello") == 99162322  # Java's "hello".hashCode()
    assert recent._java_hash("a" * 50) < 0  # wraps like a signed 32-bit int


def test_honours_claude_config_dir(fake_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home.tmp / "alt-claude"))
    write_session(fake_home.project, SID[0], [rec_user("hello there"), rec_assistant()])
    assert (fake_home.tmp / "alt-claude" / "projects").is_dir()
    assert [s.id for s in recent.list_recent(fake_home.project).sessions] == [SID[0]]


def test_titles_order_and_limit(fake_home):
    standard_sessions(fake_home.project)
    res = recent.list_recent(fake_home.project, 3)
    assert res.status == "ok"
    assert names(res) == [
        (SID[0], "FORCE Kafka reconciliation", "custom"),  # custom beats the AI title
        (SID[1], "Red build triage", "ai"),
        # first real prompt (the /mcp command is skipped), shortened to ~60 chars
        (SID[2], "please summarise the release notes for version 4.2 of the p…", "prompt"),
    ]
    assert len(res.sessions[2].name) <= 60
    assert res.sessions[0].custom and not res.sessions[1].custom
    assert res.sessions[0].title == "FORCE Kafka reconciliation" and res.sessions[2].title is None
    assert res.sessions[0].label() == "FORCE Kafka reconciliation · 2 h ago"
    assert [s.id for s in recent.list_recent(fake_home.project, 10).sessions] == SID


def test_order_is_by_last_activity_not_file_order(fake_home):
    # file mtimes say A is newer, but B's records are: the records win
    write_session(fake_home.project, SID[0], [rec_user("a", 10), rec_assistant(9)], hours_ago=1)
    write_session(fake_home.project, SID[1], [rec_user("b", 3), rec_assistant(0.5)], hours_ago=2)
    assert [s.id for s in recent.list_recent(fake_home.project).sessions] == [SID[1], SID[0]]


def test_last_title_wins_and_a_cleared_custom_title_falls_back(fake_home):
    write_session(fake_home.project, SID[0], [rec_user("x"), rec_assistant(), rec_custom("First"),
                                              rec_ai("Old AI"), rec_custom("Second"), rec_ai("New AI")])
    write_session(fake_home.project, SID[1], [rec_user("x"), rec_assistant(), rec_custom("Named"),
                                              rec_ai("The AI title"), rec_custom("")])
    got = {s.id: (s.name, s.source) for s in recent.list_recent(fake_home.project, 5).sessions}
    assert got[SID[0]] == ("Second", "custom")
    assert got[SID[1]] == ("The AI title", "ai")


def test_malformed_and_unknown_records_are_ignored(fake_home):
    write_session(fake_home.project, SID[0], [rec_user("real prompt", 2), {"type": "some-future-record", "x": 1},
                                              rec_assistant(1), rec_ai("Still works")],
                  raw_lines=["{not json", "[1, 2, 3]", '"just a string"', '{"no_type": true}',
                             '{"type": "user", "message": 42}', '{"type": "custom-title", "customTitle": 7}',
                             '{"type": "user", "timestamp": "not a date"}', '{"type": "assistant", "trunc'])
    res = recent.list_recent(fake_home.project)
    assert names(res) == [(SID[0], "Still works", "ai")]


def test_empty_project_and_empty_files(fake_home):
    res = recent.list_recent(fake_home.project)
    assert res.status == "empty" and res.sessions == [] and "no Claude Code sessions" in res.reason
    p = write_session(fake_home.project, SID[0], [])
    assert p.stat().st_size == 0
    assert recent.list_recent(fake_home.project).status == "empty"


def test_sidechain_only_command_only_agent_files_and_subdirs_are_skipped(fake_home):
    side = dict(isSidechain=True)
    write_session(fake_home.project, SID[0], [rec_user("sub task", **side), rec_assistant(**side)])
    write_session(fake_home.project, SID[1], [rec_user("<command-name>/mcp</command-name>"),
                                              rec_user("<local-command-stdout>ok</local-command-stdout>")])
    write_session(fake_home.project, "agent-1234", [rec_user("agent prompt"), rec_assistant()])
    d = recent.projects_root() / recent.project_key(fake_home.project)
    (d / SID[2]).mkdir()  # a session's subdirectory (subagents, tool results...)
    (d / SID[2] / "nested.jsonl").write_text(json.dumps(rec_user("nested")) + "\n")
    (d / "memory").mkdir()
    res = recent.list_recent(fake_home.project)
    assert res.status == "empty" and res.sessions == []
    write_session(fake_home.project, SID[3], [rec_user("the real one"), rec_assistant()])
    assert [s.id for s in recent.list_recent(fake_home.project).sessions] == [SID[3]]


def test_nothing_understood_reports_unavailable(fake_home):
    write_session(fake_home.project, SID[0], [{"type": "v9-event", "payload": {}}])
    write_session(fake_home.project, SID[1], [], raw_lines=["garbage", "more garbage"])
    res = recent.list_recent(fake_home.project)
    assert res.status == "unavailable" and res.sessions == [] and "format" in res.reason


def test_unreadable_file_does_not_hide_the_others(fake_home):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    bad = write_session(fake_home.project, SID[0], [rec_user("x"), rec_assistant()], hours_ago=0.1)
    bad.chmod(0)
    try:
        write_session(fake_home.project, SID[1], [rec_user("y"), rec_assistant(), rec_ai("Readable")], hours_ago=1)
        assert names(recent.list_recent(fake_home.project)) == [(SID[1], "Readable", "ai")]
    finally:
        bad.chmod(0o600)


def test_any_internal_error_reports_unavailable(fake_home, monkeypatch):
    write_session(fake_home.project, SID[0], [rec_user("x"), rec_assistant()])

    def boom(*a, **k):
        raise RuntimeError("surprise")

    monkeypatch.setattr(recent, "_candidates", boom)
    res = recent.list_recent(fake_home.project)
    assert res.status == "unavailable" and "surprise" in res.reason


def test_missing_timestamps_fall_back_to_the_file_time(fake_home):
    p = write_session(fake_home.project, SID[0], [{"type": "user", "message": {"content": "no ts"}},
                                                  {"type": "assistant", "message": {"content": []}}], hours_ago=30)
    s = recent.list_recent(fake_home.project).sessions[0]
    assert abs(s.last_activity.timestamp() - p.stat().st_mtime) < 1 and s.age() == "1 d ago"


def _big_session(cwd, sid, head, tail, filler_mb=3):
    """A transcript with `head` records, ~filler_mb MB of conversation, then `tail` records."""
    filler = [json.dumps(rec_assistant(5, padding="z" * 2000)) for _ in range(filler_mb * 500)]
    return write_session(cwd, sid, head, raw_lines=filler + [json.dumps(r) for r in tail])


def test_large_file_tail_first_and_bounded(fake_home, monkeypatch):
    p = _big_session(fake_home.project, SID[0], [rec_user("first prompt", 9)], [rec_ai("AI"), rec_custom("Tail name")])
    reads = []
    real_open = open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def read(self, n=-1):
            data = self.fh.read(n)
            reads.append(len(data))
            return data

        def __getattr__(self, k):
            return getattr(self.fh, k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.fh.__exit__(*a)

    def spy_open(path, mode="r", *a, **k):
        assert "w" not in mode and "a" not in mode and "+" not in mode, "recent.py must never write"
        fh = real_open(path, mode, *a, **k)
        return Spy(fh) if str(path) == str(p) else fh

    monkeypatch.setattr("builtins.open", spy_open)
    s = recent.read_session(p)
    assert (s.name, s.source) == ("Tail name", "custom")
    assert sum(reads) == recent.TAIL_FIRST  # found in the first tail window: nothing else read
    assert p.stat().st_size > 2 * recent.TAIL_MAX


def test_large_file_title_only_near_the_start(fake_home):
    # a rename early on that was never re-appended: the head window finds it
    p = _big_session(fake_home.project, SID[0], [rec_user("first prompt", 9), rec_custom("Early name")], [rec_ai("AI")])
    s = recent.read_session(p)
    assert (s.name, s.source) == ("Early name", "custom")
    # no title anywhere near the ends: the first prompt (from the head window) names it
    p = _big_session(fake_home.project, SID[1], [rec_user("what the session is about", 9)], [rec_assistant(1)])
    assert (recent.read_session(p).name, recent.read_session(p).source) == ("what the session is about", "prompt")


def test_custom_title_sidecar_is_the_fallback(fake_home):
    p = _big_session(fake_home.project, SID[0], [rec_user("first prompt", 9)], [rec_ai("AI title")])
    (p.parent / SID[0]).mkdir()
    (p.parent / SID[0] / "custom-title.json").write_text(json.dumps({"customTitle": "From sidecar"}))
    s = recent.read_session(p)
    assert (s.name, s.source) == ("From sidecar", "custom")


def test_reading_never_changes_claudes_files(fake_home):
    standard_sessions(fake_home.project)
    root = recent.projects_root()

    def snapshot():
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size, p.read_bytes())
                for p in root.rglob("*") if p.is_file()}

    before = snapshot()
    recent.list_recent(fake_home.project, 10)
    recent.find(fake_home.project, SID[0])
    assert snapshot() == before


def test_find_by_id(fake_home):
    standard_sessions(fake_home.project)
    assert recent.find(fake_home.project, SID[3]).name == "Oldest named"
    assert recent.find(fake_home.project, "nope-0000") is None
    for bad in ("../x", "", "-rf", "a/b", "x" * 200):
        assert recent.find(fake_home.project, bad) is None


def test_only_this_projects_sessions(fake_home):
    other = fake_home.tmp / "work" / "other"
    other.mkdir()
    write_session(other, SID[0], [rec_user("elsewhere"), rec_assistant()])
    write_session(fake_home.project, SID[1], [rec_user("here"), rec_assistant()])
    assert [s.id for s in recent.list_recent(fake_home.project).sessions] == [SID[1]]


def test_age_strings():
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    assert recent.age(now - timedelta(seconds=20), now) == "just now"
    assert recent.age(now - timedelta(minutes=7), now) == "7 min ago"
    assert recent.age(now - timedelta(hours=2, minutes=59), now) == "2 h ago"
    assert recent.age(now - timedelta(days=3), now) == "3 d ago"
    assert len(recent.age(now - timedelta(days=90), now)) == 10  # a date


def test_to_json_has_no_conversation_content_beyond_the_fallback_name(fake_home):
    write_session(fake_home.project, SID[0], [rec_user("secret prompt " + "x" * 200, 2), rec_assistant(1),
                                              rec_user("second prompt must not appear", 0.5)])
    doc = json.dumps([s.to_json() for s in recent.list_recent(fake_home.project).sessions])
    assert "second prompt" not in doc and "x" * 61 not in doc
    assert set(json.loads(doc)[0]) == {"id", "name", "name_source", "custom_name", "last_activity", "age"}


def test_ts_helper_is_parsed(fake_home):
    assert recent._parse_ts(ts(1)).tzinfo is not None
    assert recent._parse_ts("2026-10-17T09:49:00").tzinfo == timezone.utc
