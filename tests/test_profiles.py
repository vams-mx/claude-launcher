import pytest

from claude_launcher import profiles
from claude_launcher.plan import Selection


def test_save_load_delete_default(fake_home):
    sel = Selection(local=["agentmemory", "gdrive"], model="sonnet", resume_id="should-not-persist")
    path = profiles.save("memory+gdrive", sel)
    assert path.parent == fake_home.home / ".claude-launcher/profiles"
    loaded = profiles.load("memory+gdrive")
    assert loaded.local == ["agentmemory", "gdrive"] and loaded.model == "sonnet" and loaded.resume_id is None
    assert profiles.list_profiles() == ["memory+gdrive"]
    profiles.set_default("memory+gdrive")
    assert profiles.get_default() == "memory+gdrive"
    profiles.delete("memory+gdrive")
    assert profiles.list_profiles() == [] and profiles.get_default() is None


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "x" * 80])
def test_invalid_names(fake_home, bad):
    with pytest.raises(profiles.ProfileError):
        profiles.save(bad, Selection())


def test_missing(fake_home):
    with pytest.raises(profiles.ProfileError):
        profiles.load("nope")
    with pytest.raises(profiles.ProfileError):
        profiles.set_default("nope")


def test_reconcile_drops_vanished_items(catalog):
    sel = Selection(local=["agentmemory", "gone"], connectors=["claude.ai Slack", "claude.ai Vanished"],
                    plugins=["prompt-tools@market", "x@y"], cmux=True)
    out, notes = profiles.reconcile(sel, catalog)
    assert out.local == ["agentmemory"] and out.connectors == ["claude.ai Slack"]
    assert out.plugins == ["prompt-tools@market"] and out.cmux is True
    assert len(notes) == 3


@pytest.mark.parametrize("content", ["[]", "null", "42", None])  # None: unreadable (a directory)
def test_malformed_or_unreadable_profile_is_a_profile_error(fake_home, content):
    path = profiles.profiles_dir() / "bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        path.mkdir()
    else:
        path.write_text(content)
    with pytest.raises(profiles.ProfileError, match="bad"):
        profiles.load("bad")
