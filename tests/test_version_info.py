"""L1: services/version_info — reading the running version without git on the PATH."""
import services.version_info as vi


def _no_git(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(vi, "_git", boom)


def test_reads_a_loose_branch_ref(tmp_path, monkeypatch):
    _no_git(monkeypatch)
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text("82bf6e1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
    info = vi.version_info(tmp_path)
    assert (info["branch"], info["sha"]) == ("main", "82bf6e1")
    assert vi.version_label(info) == "main · 82bf6e1"


def test_reads_a_packed_ref(tmp_path, monkeypatch):
    _no_git(monkeypatch)
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "packed-refs").write_text("# pack-refs with: peeled\n"
                                   "5768a95bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb refs/heads/main\n")
    assert vi.version_info(tmp_path)["sha"] == "5768a95"


def test_reads_a_worktree_through_its_gitdir_file(tmp_path, monkeypatch):
    _no_git(monkeypatch)
    common = tmp_path / "repo" / ".git"
    wt_dir = common / "worktrees" / "wt"
    wt_dir.mkdir(parents=True)
    (common / "refs" / "heads" / "feat").mkdir(parents=True)
    (common / "refs" / "heads" / "feat" / "x").write_text("53524c1ccccccccccccccccccccccccccccccccc\n")
    (wt_dir / "HEAD").write_text("ref: refs/heads/feat/x\n")
    (wt_dir / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_dir}\n")
    info = vi.version_info(wt)
    assert (info["branch"], info["sha"]) == ("feat/x", "53524c1")


def test_a_downloaded_copy_says_why_it_has_no_version(tmp_path, monkeypatch):
    _no_git(monkeypatch)
    label = vi.version_label(vi.version_info(tmp_path))
    assert label.startswith("Version unknown") and "not a git checkout" in label
