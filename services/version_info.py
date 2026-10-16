"""Which version of the tool is running: branch, commit and commit date.

Shown in the app so a screenshot answers "is this machine on the latest?". Asks git first; if git
isn't on the PATH, reads the checkout's .git folder directly (branch and commit only).
"""
import subprocess
from pathlib import Path


def _git(repo: Path, *args) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         timeout=5, check=True)
    return out.stdout.strip()


def _git_dir(repo: Path):
    """(git dir, common dir) for a checkout or a worktree, or (None, None)."""
    dot = repo / ".git"
    if dot.is_dir():
        return dot, dot
    if dot.is_file():
        line = dot.read_text().strip()
        if line.startswith("gitdir:"):
            gd = (repo / line.split(":", 1)[1].strip()).resolve()
            common = gd
            cf = gd / "commondir"
            if cf.is_file():
                common = (gd / cf.read_text().strip()).resolve()
            return gd, common
    return None, None


def _read_ref(common: Path, ref: str) -> str:
    loose = common / ref
    if loose.is_file():
        return loose.read_text().strip()
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return ""


def version_info(repo) -> dict:
    """{"branch", "sha", "date", "error"}. Values are "" when unknown; `error` says why."""
    repo = Path(repo)
    try:
        sha = _git(repo, "rev-parse", "--short", "HEAD")
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        date = _git(repo, "log", "-1", "--format=%cd", "--date=format:%d %b %Y %H:%M")
        return {"branch": branch, "sha": sha, "date": date, "error": ""}
    except Exception:
        pass
    gd, common = _git_dir(repo)
    if not gd:
        return {"branch": "", "sha": "", "date": "",
                "error": "not a git checkout (a downloaded copy has no version to read)"}
    try:
        head = (gd / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            branch = ref.rsplit("refs/heads/", 1)[-1]
            sha = _read_ref(common, ref)
        else:
            branch, sha = "(detached)", head
        return {"branch": branch, "sha": sha[:7], "date": "", "error": ""}
    except Exception as e:
        return {"branch": "", "sha": "", "date": "", "error": f"couldn't read .git: {e}"}


def version_label(info: dict) -> str:
    if not info.get("sha"):
        return f"Version unknown: {info.get('error') or 'no commit found'}"
    return " · ".join(x for x in (info.get("branch"), info.get("sha"), info.get("date")) if x)
