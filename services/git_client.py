"""
GitHub operations via PyGitHub.
Handles branch management, file commits, PR creation and merge.
"""

import base64
from typing import Dict, Optional
from github import Github, GithubException, InputGitTreeElement, Auth


class GitClient:
    DEV_BRANCH  = "dev"
    MAIN_BRANCH = "main"

    def __init__(self, token: str, repo_name: str):
        self._gh   = Github(auth=Auth.Token(token))
        self._repo = self._gh.get_repo(repo_name)

    # ── Commit TML files ──────────────────────────────────────────────────────

    def commit_tml(self, team: str, files: Dict[str, str],
                   message: Optional[str] = None) -> str:
        """
        Commit {relative_path: yaml_string} under the team/ folder on the dev branch and
        return the commit SHA. dev is a disposable staging branch for the dev->main PR, so
        each export bases its commit on the CURRENT main tip and force-points dev at it.

        Basing on main (rather than re-reading dev) and forcing the ref update avoids two
        failure modes that both surface as 422 "Update is not a fast forward": a stale dev
        read right after a reset, and divergence after a prior PR was squash-merged into main.

        files keys look like:  models/vbu_sales_v.model.tml
        Written to git as:     vbu/models/vbu_sales_v.model.tml
        """
        commit_message = message or f"chore: export TML for team {team}"
        base_commit    = self._repo.get_branch(self.MAIN_BRANCH).commit  # fresh main tip

        # Build tree blobs on top of main's tree
        blobs = []
        for rel_path, content in files.items():
            git_path = f"{team.lower()}/{rel_path}"
            blob = self._repo.create_git_blob(content, "utf-8")
            blobs.append(InputGitTreeElement(
                path=git_path,
                mode="100644",
                type="blob",
                sha=blob.sha,
            ))

        base_tree  = self._repo.get_git_tree(base_commit.commit.tree.sha)
        new_tree   = self._repo.create_git_tree(blobs, base_tree)
        new_commit = self._repo.create_git_commit(
            commit_message,
            new_tree,
            [base_commit.commit],
        )

        # Point dev at the new commit. force=True because dev is disposable and may have
        # diverged from main (e.g. after a squash-merge); create it if it does not exist.
        try:
            self._repo.get_git_ref(f"heads/{self.DEV_BRANCH}").edit(new_commit.sha, force=True)
        except GithubException:
            self._repo.create_git_ref(f"refs/heads/{self.DEV_BRANCH}", new_commit.sha)

        return new_commit.sha

    # ── PR management ─────────────────────────────────────────────────────────

    def create_pr(self, team: str, commit_sha: str) -> str:
        """
        Open a PR from dev → main. Returns the PR URL.
        Re-uses an existing open PR if one already exists.
        """
        # Check for open PR already
        for pr in self._repo.get_pulls(state="open", base=self.MAIN_BRANCH,
                                        head=self.DEV_BRANCH):
            return pr.html_url

        pr = self._repo.create_pull(
            title=f"[{team}] Cross-cluster TML promotion (source → target)",
            body=(
                f"Automated TML export for team **{team}**.\n\n"
                "Data-layer remap applied: connection / db / schema → target cluster. "
                "Object names and obj_ids are preserved.\n\n"
                f"Commit: `{commit_sha}`\n\n"
                "Review the TML files below before merging."
            ),
            head=self.DEV_BRANCH,
            base=self.MAIN_BRANCH,
        )
        return pr.html_url

    def get_open_pr(self) -> Optional[object]:
        """Return the first open PR from dev → main, or None."""
        for pr in self._repo.get_pulls(state="open", base=self.MAIN_BRANCH,
                                        head=self.DEV_BRANCH):
            return pr
        return None

    def merge_pr(self) -> bool:
        """Merge the open dev → main PR. Returns True on success."""
        pr = self.get_open_pr()
        if not pr:
            return False
        pr.merge(merge_method="squash",
                 commit_title=pr.title,
                 commit_message="Merged via migration tool.")
        return True

    # ── Pull TML from main ────────────────────────────────────────────────────

    def get_tml_files(self, team: str, branch: Optional[str] = None) -> Dict[str, str]:
        """
        Pull all .tml files for a team from the given branch (default: main).
        Returns {relative_path: yaml_string} (relative to team folder).
        """
        ref   = branch or self.MAIN_BRANCH
        files = {}
        team_path = team.lower()
        try:
            contents = self._repo.get_contents(team_path, ref=ref)
        except GithubException:
            return files

        # Recurse into sub-folders (models/, liveboards/, answers/)
        queue = list(contents)
        while queue:
            item = queue.pop(0)
            if item.type == "dir":
                queue.extend(self._repo.get_contents(item.path, ref=ref))
            elif item.name.endswith(".tml"):
                # Strip the team prefix to get relative path
                rel_path = item.path[len(team_path) + 1:]
                files[rel_path] = item.decoded_content.decode("utf-8")

        return files
