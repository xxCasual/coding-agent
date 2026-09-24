from pathlib import Path

from git import Repo


class RepoCache:
    """Local cache for cloned repositories."""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)

    def path_for(self, owner: str, repo: str, ref: str) -> Path:
        safe_ref = ref.replace("/", "_")
        return self.cache_dir / f"{owner}__{repo}__{safe_ref}"

    def clone_or_update(self, clone_url: str, owner: str, repo: str, ref: str) -> Path:
        target = self.path_for(owner, repo, ref)
        if target.exists():
            repository = Repo(target)
            repository.remotes.origin.fetch()
            repository.git.checkout(ref)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        repository = Repo.clone_from(clone_url, target)
        repository.git.checkout(ref)
        return target
