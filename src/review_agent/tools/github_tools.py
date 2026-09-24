import re
import logging
import time
from typing import Protocol

import httpx

from review_agent.config import Settings, get_settings
from review_agent.errors import GitHubRequestError, InvalidInputError
from review_agent.models.pr import PRLocator, PRMeta
from review_agent.observability import get_logger, log_event

PR_URL_RE = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$")
logger = get_logger(__name__)


def parse_pr_url_tool(pr_url: str) -> PRLocator:
    match = PR_URL_RE.match(pr_url.strip())
    if not match:
        raise InvalidInputError(
            "Expected a GitHub pull request URL like https://github.com/owner/repo/pull/123",
            public_message="Expected a GitHub pull request URL like https://github.com/owner/repo/pull/123.",
        )
    return PRLocator(
        owner=match.group("owner"),
        repo=match.group("repo"),
        pr_number=int(match.group("number")),
    )


class GitHubClient(Protocol):
    def fetch_pr_meta(self, locator: PRLocator) -> PRMeta:
        ...

    def fetch_pr_diff(self, locator: PRLocator) -> str:
        ...

    def fetch_file_at_ref(self, locator: PRLocator, file_path: str, ref: str) -> str:
        ...


class HttpGitHubClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return headers

    def fetch_pr_meta(self, locator: PRLocator) -> PRMeta:
        url = f"https://api.github.com/repos/{locator.owner}/{locator.repo}/pulls/{locator.pr_number}"
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "github.fetch_pr_meta.start",
            "Fetching GitHub PR metadata",
            stage="github.fetch_pr_meta",
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
        )
        with httpx.Client(timeout=30) as client:
            payload = self._get_json(
                client,
                url,
                locator,
                stage="github.fetch_pr_meta",
                started=started,
                description="PR metadata",
                public_message="Could not fetch pull request metadata from GitHub.",
            )
            files = self._fetch_pr_files(client, url, locator, started)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.INFO,
            "github.fetch_pr_meta.success",
            "Fetched GitHub PR metadata",
            stage="github.fetch_pr_meta",
            duration_ms=duration_ms,
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
        )
        return PRMeta(
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
            title=payload.get("title", ""),
            author=(payload.get("user") or {}).get("login", ""),
            base_ref=(payload.get("base") or {}).get("ref", ""),
            head_ref=(payload.get("head") or {}).get("ref", ""),
            base_sha=(payload.get("base") or {}).get("sha", ""),
            head_sha=(payload.get("head") or {}).get("sha", ""),
            changed_files=[item["filename"] for item in files],
            html_url=payload.get("html_url", ""),
            clone_url=((payload.get("base") or {}).get("repo") or {}).get("clone_url"),
            head_clone_url=((payload.get("head") or {}).get("repo") or {}).get("clone_url"),
            head_repo_full_name=((payload.get("head") or {}).get("repo") or {}).get("full_name"),
        )

    def fetch_pr_diff(self, locator: PRLocator) -> str:
        url = f"https://github.com/{locator.owner}/{locator.repo}/pull/{locator.pr_number}.diff"
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "github.fetch_pr_diff.start",
            "Fetching GitHub PR diff",
            stage="github.fetch_pr_diff",
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
        )
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = self._get_response(
                client,
                url,
                locator,
                stage="github.fetch_pr_diff",
                started=started,
                description="PR diff",
                public_message="Could not fetch pull request diff from GitHub.",
                accept="application/vnd.github.v3.diff",
            )
            diff = response.text
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.INFO,
            "github.fetch_pr_diff.success",
            "Fetched GitHub PR diff",
            stage="github.fetch_pr_diff",
            duration_ms=duration_ms,
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
        )
        return diff

    def fetch_file_at_ref(self, locator: PRLocator, file_path: str, ref: str) -> str:
        url = f"https://raw.githubusercontent.com/{locator.owner}/{locator.repo}/{ref}/{file_path}"
        started = time.perf_counter()
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = self._get_response(
                client,
                url,
                locator,
                stage="github.fetch_file",
                started=started,
                description=f"file {file_path}",
                public_message=f"Could not fetch {file_path} from GitHub.",
                accept="text/plain",
                metadata={"file_path": file_path},
            )
            return response.text

    def _fetch_pr_files(
        self,
        client: httpx.Client,
        pr_url: str,
        locator: PRLocator,
        started: float,
    ) -> list[dict]:
        files: list[dict] = []
        page = 1
        per_page = 100
        while True:
            batch = self._get_json(
                client,
                f"{pr_url}/files",
                locator,
                stage="github.fetch_pr_meta",
                started=started,
                description="PR files",
                public_message="Could not fetch pull request metadata from GitHub.",
                params={"per_page": per_page, "page": page},
            )
            files.extend(batch)
            if len(batch) < per_page:
                return files
            page += 1

    def _get_json(
        self,
        client: httpx.Client,
        url: str,
        locator: PRLocator,
        *,
        stage: str,
        started: float,
        description: str,
        public_message: str,
        accept: str = "application/vnd.github+json",
        params: dict | None = None,
        metadata: dict | None = None,
    ):
        response = self._get_response(
            client,
            url,
            locator,
            stage=stage,
            started=started,
            description=description,
            public_message=public_message,
            accept=accept,
            params=params,
            metadata=metadata,
        )
        return response.json()

    def _get_response(
        self,
        client: httpx.Client,
        url: str,
        locator: PRLocator,
        *,
        stage: str,
        started: float,
        description: str,
        public_message: str,
        accept: str = "application/vnd.github+json",
        params: dict | None = None,
        metadata: dict | None = None,
    ) -> httpx.Response:
        try:
            response = client.get(url, headers=self._headers(accept), params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            _log_github_failure(f"{stage}.failure", started, locator, exc)
            raise GitHubRequestError(
                f"GitHub returned {exc.response.status_code} for {description}.",
                public_message=public_message,
                metadata={
                    "status_code": exc.response.status_code,
                    "url": str(exc.request.url),
                    "owner": locator.owner,
                    "repo": locator.repo,
                    "pr_number": locator.pr_number,
                    **(metadata or {}),
                },
            ) from exc
        except httpx.HTTPError as exc:
            _log_github_failure(f"{stage}.failure", started, locator, exc)
            raise GitHubRequestError(
                f"GitHub request failed for {description}: {exc}",
                public_message=public_message,
                metadata={
                    "exception_type": type(exc).__name__,
                    "owner": locator.owner,
                    "repo": locator.repo,
                    "pr_number": locator.pr_number,
                    **(metadata or {}),
                },
            ) from exc


def fetch_pr_meta_tool(pr_url: str, client: GitHubClient | None = None) -> PRMeta:
    locator = parse_pr_url_tool(pr_url)
    return (client or HttpGitHubClient()).fetch_pr_meta(locator)


def fetch_pr_diff_tool(pr_url: str, client: GitHubClient | None = None) -> str:
    locator = parse_pr_url_tool(pr_url)
    return (client or HttpGitHubClient()).fetch_pr_diff(locator)


def fetch_file_at_ref_tool(
    pr_url: str, file_path: str, ref: str, client: GitHubClient | None = None
) -> str:
    locator = parse_pr_url_tool(pr_url)
    return (client or HttpGitHubClient()).fetch_file_at_ref(locator, file_path, ref)


def _log_github_failure(
    event: str,
    started: float,
    locator: PRLocator,
    exc: Exception,
) -> None:
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.exception(
        "GitHub request failed",
        extra={
            "event": event,
            "stage": event.removesuffix(".failure"),
            "duration_ms": duration_ms,
            "error_code": "github.request_failed",
            "owner": locator.owner,
            "repo": locator.repo,
            "pr_number": locator.pr_number,
            "exception_type": type(exc).__name__,
        },
    )
