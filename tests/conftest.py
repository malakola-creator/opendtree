"""Helpers for dtree's tests: a git repository made for the test, and a client over the API without a socket."""
import shutil
import subprocess
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from dtree.project import Project
from dtree.server import Api


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def get_json(self):
        return self._data


class ApiClient:
    """``client.get(url)`` / ``client.post(url, json=…)`` straight into ``server.Api`` (the routes, apart from HTTP)."""

    def __init__(self, project):
        self.api = Api(project)

    def _go(self, method, url, body=None):
        u = urlparse(url)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        return _Resp(*self.api.handle(method, unquote(u.path), q, body or {}))

    def get(self, url):
        return self._go("GET", url)

    def post(self, url, json=None):
        return self._go("POST", url, json)


def client_for(store, root="."):
    p = Project(root, {})
    p._store = store
    return ApiClient(p)


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("no git")
    r = tmp_path / "repo"
    r.mkdir()
    def git(*a):
        return subprocess.run(["git", "-C", str(r)] + list(a), capture_output=True, text=True, check=True).stdout
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid"); git("config", "user.name", "t")
    (r / "f.txt").write_text("one\n")
    git("add", "f.txt"); git("commit", "-q", "-m", "first")
    return {"path": r, "git": git, "tmp": tmp_path}
