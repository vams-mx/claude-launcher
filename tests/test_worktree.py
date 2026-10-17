"""Worktrees (worktree.py, and the worktree parts of recent.py): names, checkouts, existing names.

Git layouts are written by hand (a linked worktree is a `.git` file pointing at
`<main>/.git/worktrees/<id>`, which has `gitdir` and `commondir`), so no git
process ever runs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from claude_launcher import recent
from claude_launcher import worktree as wt
from tests.conftest import SID, rec_assistant, rec_custom, rec_user, write_session


def add_worktree(main: Path, path: Path, *, branch: str | None = None, gid: str | None = None) -> Path:
    """Register `path` as a linked worktree of the repository at `main` (the files git writes)."""
    gid = gid or path.name
    meta = main / ".git" / "worktrees" / gid
    meta.mkdir(parents=True)
    (meta / "gitdir").write_text(f"{path}/.git\n")
    (meta / "commondir").write_text("../..\n")
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text(f"gitdir: {meta}\n")
    if branch:
        ref = main / ".git" / "refs" / "heads" / branch
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text("0" * 40 + "\n")
    return path


# ---------------------------------------------------------------------------
# names


@pytest.mark.parametrize("text,expected", [
    ("FORCE Kafka reconciliation", "force-kafka-reconciliation"),
    ("X (fork)", "x-fork"),
    ("X v2", "x-v2"),
    ("  --Hello__World!!  ", "hello-world"),
    ("Café crème/ñandú", "cafe-creme-nandu"),
    ("feature/login.page", "feature-login-page"),
    ("🙂", ""),
    ("", ""),
    (None, ""),
])
def test_slug(text, expected):
    assert wt.slug(text) == expected


def test_slug_is_capped_without_a_trailing_dash():
    s = wt.slug("a" * 63 + " b" * 10)
    assert len(s) <= wt.NAME_MAX and not s.endswith("-")
    assert wt.slug("word " * 40, max_len=10) == "word-word"


def test_slugs_are_valid_names():
    for text in ("FORCE Kafka reconciliation", "X (fork)", "a" * 200, "Ünïcödé"):
        assert wt.valid_name(wt.slug(text)) is None


def test_generated_and_default_names():
    g = wt.generated_name(datetime(2026, 10, 17, 9, 30), "3fa2")
    assert g == "cm-20261017-3fa2" and wt.valid_name(g) is None
    assert len(wt.generated_name().split("-")[-1]) == 4
    assert wt.default_name("X (fork)", g) == "x-fork"
    assert wt.default_name(None, g) == g and wt.default_name("!!!", g) == g


# ---------------------------------------------------------------------------
# checkouts


def test_checkouts_of_a_repository(fake_home):
    main = fake_home.project
    a = add_worktree(main, main / ".claude" / "worktrees" / "feat-a", branch="worktree-feat-a")
    b = add_worktree(main, fake_home.tmp / "elsewhere" / "hotfix")
    (main / ".claude" / "worktrees" / "stray").mkdir()  # no .git: not a checkout
    gone = main / ".git" / "worktrees" / "gone"  # registered, directory removed
    gone.mkdir()
    (gone / "gitdir").write_text(f"{fake_home.tmp}/gone/.git\n")

    got = [(c.path, c.label) for c in wt.other_checkouts(main)]
    assert got == [(a, "feat-a"), (b, "hotfix")]
    # from a worktree: the main checkout and the other worktrees
    got = [(c.path, c.label) for c in wt.other_checkouts(a / "src")]
    assert got == [(main, "proj"), (b, "hotfix")]
    repo = wt.find_repo(a)
    assert (repo.root, repo.toplevel) == (main, a)


def test_no_repository_no_checkouts(fake_home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert wt.find_repo(plain) is None and wt.other_checkouts(plain) == []


def test_checkouts_are_bounded(fake_home, monkeypatch):
    monkeypatch.setattr(wt, "MAX_CHECKOUTS", 3)
    for i in range(6):
        add_worktree(fake_home.project, fake_home.project / ".claude" / "worktrees" / f"w{i}")
    assert len(wt.other_checkouts(fake_home.project)) == 3


# ---------------------------------------------------------------------------
# Recent with worktrees


def test_recent_lists_worktree_sessions_marked(fake_home):
    main = fake_home.project
    a = add_worktree(main, main / ".claude" / "worktrees" / "feat-a")
    write_session(main, SID[0], [rec_user("main work", 3), rec_assistant(3), rec_custom("Main thing")], hours_ago=3)
    write_session(a, SID[1], [rec_user("wt work", 1), rec_assistant(1), rec_custom("Worktree thing")], hours_ago=1)

    res = recent.list_recent(main, 5)
    assert [(s.id, s.worktree, s.cwd) for s in res.sessions] == [(SID[1], "feat-a", str(a)), (SID[0], None, None)]
    assert res.sessions[0].label().endswith("· ⎇ feat-a")
    assert res.sessions[0].to_json()["worktree"] == "feat-a"
    assert [s.id for s in recent.list_recent(main, 5, worktrees=False).sessions] == [SID[0]]

    # seen from the worktree, the main checkout's sessions are the marked ones
    res = recent.list_recent(a, 5)
    assert [(s.id, s.worktree, s.cwd) for s in res.sessions] == [(SID[1], None, None), (SID[0], "proj", str(main))]

    found = recent.find(main, SID[1])
    assert found and found.worktree == "feat-a" and found.cwd == str(a)
    assert recent.find(main, SID[1], worktrees=False) is None


def test_recent_worktree_reads_stay_bounded(fake_home, monkeypatch):
    main = fake_home.project
    reads = []
    real = recent.read_session
    monkeypatch.setattr(recent, "read_session", lambda p: reads.append(p) or real(p))
    for i in range(4):
        w = add_worktree(main, main / ".claude" / "worktrees" / f"w{i}")
        for j in range(20):
            sid = f"{i:08d}-0000-4000-8000-{j:012d}"
            write_session(w, sid, [rec_user(f"p{i}{j}", 1 + j), rec_assistant(1 + j)], hours_ago=1 + j)
    res = recent.list_recent(main, 3)
    assert res.status == "ok" and len(res.sessions) == 3
    assert len(reads) <= 3 + recent.SCAN_SLACK


@pytest.mark.parametrize("name", ["x", "x-v2", "feature/login", "a.b_c-d", "A" * 64, "v1.2"])
def test_valid_names(name):
    assert wt.valid_name(name) is None


@pytest.mark.parametrize("name,why", [
    ("", "empty"), ("a" * 65, "64 characters"), ("bad name", "only letters"), ("a//b", "only letters"),
    ("/a", "only letters"), ("..", "'.' and '..'"), ("a/./b", "'.' and '..'"), (".git", "reserved"),
    ("x/.GIT..", "reserved"), ("#123", "only letters"),
    ("-w", "start with '-'"),  # claude would take it for an option: -w's value is optional
])
def test_invalid_names(name, why):
    assert why in wt.valid_name(name)


def test_paths_and_branches_follow_claude():
    assert wt.dir_name("feature/login") == "feature+login"
    assert wt.branch_name("feature/login") == "worktree-feature+login"


# ---------------------------------------------------------------------------
# an existing name


def repo_of(fake_home):
    return wt.find_repo(fake_home.project)


def test_inspect_new_and_reuse(fake_home):
    main = fake_home.project
    remote_head = main / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    remote_head.parent.mkdir(parents=True)
    remote_head.write_text("ref: refs/remotes/origin/develop\n")
    st = wt.inspect(repo_of(fake_home), "x")
    assert (st.kind, st.path, st.branch) == (wt.NEW, main / ".claude" / "worktrees" / "x", "worktree-x")
    assert not st.exists and not st.needs_confirm

    add_worktree(main, main / ".claude" / "worktrees" / "x", branch="worktree-x")
    (main / ".git" / "worktrees" / "x" / "HEAD").write_text("ref: refs/heads/worktree-x\n")
    st = wt.inspect(repo_of(fake_home), "x")
    assert st.kind == wt.REUSE and st.needs_confirm
    assert "reuses it" in st.message and "git reset --hard to origin/develop" in st.message


def test_inspect_leftover_branch_is_replaced(fake_home):
    main = fake_home.project
    ref = main / ".git" / "refs" / "heads" / "worktree-old"
    ref.parent.mkdir(parents=True)
    ref.write_text("0" * 40 + "\n")
    st = wt.inspect(repo_of(fake_home), "old")
    assert st.kind == wt.REPLACE and st.needs_confirm and "-B" in st.message and "reflog" in st.message
    # a packed ref counts too
    ref.unlink()
    (main / ".git" / "packed-refs").write_text("# pack-refs with: peeled\n" + "1" * 40 + " refs/heads/worktree-old\n")
    assert wt.inspect(repo_of(fake_home), "old").kind == wt.REPLACE
    assert wt.inspect(repo_of(fake_home), "ol").kind == wt.NEW  # no prefix matches


def test_inspect_branch_checked_out_elsewhere_is_refused(fake_home):
    main = fake_home.project
    other = add_worktree(main, fake_home.tmp / "other", branch="worktree-busy")
    (main / ".git" / "worktrees" / "other" / "HEAD").write_text("ref: refs/heads/worktree-busy\n")
    st = wt.inspect(repo_of(fake_home), "busy")
    assert st.kind == wt.REFUSE and "checked out" in st.message and not st.needs_confirm
    assert other.name in st.message
    # the main checkout's own branch
    (main / ".git" / "HEAD").write_text("ref: refs/heads/worktree-mine\n")
    (main / ".git" / "refs" / "heads" / "worktree-mine").write_text("0" * 40)
    assert wt.inspect(repo_of(fake_home), "mine").kind == wt.REFUSE


def test_inspect_broken_and_foreign_directories(fake_home):
    main = fake_home.project
    wts = main / ".claude" / "worktrees"
    (wts / "plain").mkdir(parents=True)
    assert wt.inspect(repo_of(fake_home), "plain").kind == wt.REFUSE  # not a worktree
    (wts / "orphan").mkdir()
    (wts / "orphan" / ".git").write_text(f"gitdir: {main}/.git/worktrees/orphan\n")  # metadata gone
    st = wt.inspect(repo_of(fake_home), "orphan")
    assert st.kind == wt.REPLACE and "deletes the directory" in st.message
    foreign = fake_home.tmp / "elsewhere" / ".git" / "worktrees" / "f"
    foreign.mkdir(parents=True)
    (wts / "f").mkdir()
    (wts / "f" / ".git").write_text(f"gitdir: {foreign}\n")
    assert "another repository" in wt.inspect(repo_of(fake_home), "f").message


def test_inspect_symlinks_are_refused(fake_home):
    main = fake_home.project
    (main / ".claude" / "worktrees").mkdir(parents=True)
    (main / ".claude" / "worktrees" / "link").symlink_to(fake_home.tmp)
    st = wt.inspect(repo_of(fake_home), "link")
    assert st.kind == wt.REFUSE and "symlink" in st.message


def test_combinations():
    from claude_launcher.plan import Selection

    assert wt.combination_error(Selection(worktree="x")) is None
    assert wt.combination_error(Selection(worktree="x", tmux=True)) is None
    assert wt.combination_error(Selection(session="resume", resume_id="a", worktree="x")) is None
    assert wt.combination_error(Selection(session="resume", resume_id="a", fork=True, worktree="x", tmux=True)) is None
    assert "Continue" in wt.combination_error(Selection(session="continue", worktree="x"))
    assert "Continue" in wt.combination_error(Selection(session="continue", fork=True, worktree="x"))
    assert "needs Fork" in wt.combination_error(Selection(session="resume", resume_id="a", worktree="x", tmux=True))
    assert wt.combination_error(Selection()) is None
