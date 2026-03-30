from __future__ import annotations

from norn.contrib.sources.jira_source import JiraSource


class Jira:
    """Fluent builder for JiraSource."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._projects: list[str] = []
        self._auth = "api_token"
        self._comments = True
        self._attachments = True
        self._stacktraces = True
        self._attachment_dir = "/tmp/issueprocessing/attachments"

    def projects(self, *keys: str) -> Jira:
        self._projects.extend(keys)
        return self

    def auth(self, method: str) -> Jira:
        self._auth = method
        return self

    def include_comments(self, v: bool) -> Jira:
        self._comments = v
        return self

    def include_attachments(self, v: bool) -> Jira:
        self._attachments = v
        return self

    def extract_stacktraces(self, v: bool) -> Jira:
        self._stacktraces = v
        return self

    def attachment_dir(self, path: str) -> Jira:
        self._attachment_dir = path
        return self

    def build(self) -> JiraSource:
        return JiraSource(
            url=self._url,
            projects=self._projects,
            auth_method=self._auth,
            include_comments=self._comments,
            include_attachments=self._attachments,
            extract_stacktraces_flag=self._stacktraces,
            attachment_dir=self._attachment_dir,
        )
