import pytest

from review_agent.errors import InvalidInputError
from review_agent.tools.diff_tools import detect_language_tool, map_line_numbers_tool, parse_diff_hunks_tool
from review_agent.tools.github_tools import HttpGitHubClient, parse_pr_url_tool


RAW_DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 def greet(name):
+    if not name:
+        return "hi"
     return f"hello {name}"
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
index 3333333..0000000
--- a/src/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def old():
-    return True
diff --git a/src/new.py b/src/new.py
new file mode 100644
index 0000000..4444444
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+def new():
+    return True
diff --git a/src/name.py b/src/renamed.py
similarity index 91%
rename from src/name.py
rename to src/renamed.py
index 5555555..6666666 100644
--- a/src/name.py
+++ b/src/renamed.py
@@ -1,2 +1,2 @@
 def name():
-    return "old"
+    return "new"
"""


def test_parse_pr_url_accepts_github_pull_urls() -> None:
    parsed = parse_pr_url_tool("https://github.com/octo/demo/pull/123")
    assert parsed.owner == "octo"
    assert parsed.repo == "demo"
    assert parsed.pr_number == 123


def test_parse_pr_url_rejects_non_pr_urls() -> None:
    with pytest.raises(InvalidInputError):
        parse_pr_url_tool("https://github.com/octo/demo/issues/123")


def test_parse_diff_hunks_covers_change_types_and_languages() -> None:
    hunks = parse_diff_hunks_tool(RAW_DIFF)
    change_types = {h.file_path: h.change_type for h in hunks}

    assert change_types["src/app.py"] == "modified"
    assert change_types["src/old.py"] == "deleted"
    assert change_types["src/new.py"] == "added"
    assert change_types["src/renamed.py"] == "renamed"
    assert all(h.language == "python" for h in hunks)
    assert hunks[0].added_code.startswith("if not name")


def test_line_mapping_and_language_detection() -> None:
    hunk = parse_diff_hunks_tool(RAW_DIFF)[0]
    mapping = map_line_numbers_tool(hunk)
    assert mapping["new_to_old"][2] is None
    assert mapping["new_to_old"][4] == 3
    assert detect_language_tool("README.md") == "unknown"


def test_github_client_paginates_changed_files(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
            self.text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class PagingClient:
        calls = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def get(self, url, headers=None, params=None):
            self.calls.append((url, params))
            if url.endswith("/pulls/1"):
                return FakeResponse(
                    {
                        "title": "Paged files",
                        "user": {"login": "mona"},
                        "base": {
                            "ref": "main",
                            "sha": "a" * 40,
                            "repo": {"clone_url": "https://github.com/octo/demo.git"},
                        },
                        "head": {
                            "ref": "feature",
                            "sha": "b" * 40,
                            "repo": {
                                "clone_url": "https://github.com/octo/demo.git",
                                "full_name": "octo/demo",
                            },
                        },
                        "html_url": "https://github.com/octo/demo/pull/1",
                    }
                )
            page = params["page"]
            if page == 1:
                return FakeResponse([{"filename": f"file_{index}.py"} for index in range(100)])
            return FakeResponse([{"filename": "last.py"}])

    monkeypatch.setattr("review_agent.tools.github_tools.httpx.Client", PagingClient)

    meta = HttpGitHubClient().fetch_pr_meta(parse_pr_url_tool("https://github.com/octo/demo/pull/1"))

    assert len(meta.changed_files) == 101
    assert meta.changed_files[-1] == "last.py"
    assert [params["page"] for url, params in PagingClient.calls if url.endswith("/files")] == [1, 2]
