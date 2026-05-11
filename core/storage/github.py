import base64
import json
import urllib.request
from django.conf import settings
from django.core.files.storage import Storage
from urllib.error import HTTPError


class GitHubStorage(Storage):
    """
    A Django Storage backend for uploading files to GitHub via the API.
    """

    def _save(self, name, content):
        """
        Save a file to GitHub. Overwrites if file exists.
        """
        repo = settings.GITHUB_ASSETS["REPO"]
        branch = settings.GITHUB_ASSETS["BRANCH"]
        token = settings.GITHUB_ASSETS["TOKEN"]

        api_url = f"https://api.github.com/repos/{repo}/contents/{name}"

        raw = content.read()
        encoded = base64.b64encode(raw).decode("utf-8")

        payload = {
            "message": f"Upload {name}",
            "content": encoded,
            "branch": branch,
        }

        data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            api_url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            method="PUT",
        )

        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())

        return name

    def exists(self, name):
        """
        Return True if the file exists in the GitHub repo.
        """
        repo = settings.GITHUB_ASSETS["REPO"]
        branch = settings.GITHUB_ASSETS["BRANCH"]
        token = settings.GITHUB_ASSETS["TOKEN"]

        api_url = f"https://api.github.com/repos/{repo}/contents/{name}?ref={branch}"

        request = urllib.request.Request(
            api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(request) as response:
                if response.status == 200:
                    return True
        except HTTPError as e:
            if e.code == 404:
                return False
            raise e

    def delete(self, name):
        """
        Delete a file from GitHub.
        """
        repo = settings.GITHUB_ASSETS["REPO"]
        branch = settings.GITHUB_ASSETS["BRANCH"]
        token = settings.GITHUB_ASSETS["TOKEN"]

        # First, get the file SHA (required to delete)
        api_url = f"https://api.github.com/repos/{repo}/contents/{name}?ref={branch}"

        request = urllib.request.Request(
            api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(request) as response:
                result = json.loads(response.read())
                sha = result["sha"]
        except HTTPError as e:
            if e.code == 404:
                return  # already gone
            raise e

        # Delete file
        payload = {
            "message": f"Delete {name}",
            "sha": sha,
            "branch": branch,
        }

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            api_url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            method="DELETE",
        )

        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())

        return name

    def url(self, name):
        return settings.GITHUB_ASSETS["BASE_URL"] + name


class OverwriteStorage(GitHubStorage):
    """
    GitHub storage that overwrites files with the same name instead of creating a new one.
    """

    def get_available_name(self, name, max_length=None):
        if self.exists(name):
            self.delete(name)
        return name
