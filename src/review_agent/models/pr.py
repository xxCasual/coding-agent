from pydantic import BaseModel, Field


class PRLocator(BaseModel):
    owner: str
    repo: str
    pr_number: int = Field(gt=0)


class PRMeta(BaseModel):
    owner: str
    repo: str
    pr_number: int = Field(gt=0)
    title: str
    author: str
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    changed_files: list[str]
    html_url: str
    clone_url: str | None = None
    head_clone_url: str | None = None
    head_repo_full_name: str | None = None
