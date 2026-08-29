"""Mockable GitHub API client for releases."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from projectos.errors import OrchestrationError
from projectos.github.tokens import github_token, contains_secret

GITHUB_API = "https://api.github.com"
HttpPost = Callable[[str, dict[str, str], dict[str, Any] | None, str], dict[str, Any]]


def default_http_post(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    method: str = "POST",
) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
    data = json.loads(raw or "{}")
    return data if isinstance(data, dict) else {"ok": False, "error": "invalid_json"}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_detail(message: str) -> str:
    text = str(message or "")
    if contains_secret(text):
        return "GitHub API error (details redacted)"
    return text[:240]


@dataclass(frozen=True)
class PublicationResult:
    ok: bool
    release_url: str | None
    tag: str
    asset_urls: dict[str, str]
    detail: str = ""


class GitHubClient:
    def __init__(self, *, http_post: HttpPost | None = None, token: str | None = None) -> None:
        self._http_post = http_post or default_http_post
        self._override_token = token

    def _auth_token(self) -> str:
        token = self._override_token or github_token()
        if not token:
            raise OrchestrationError("GitHub is not configured")
        return token

    def validate_repository(self, owner: str, name: str) -> dict[str, Any]:
        token = self._auth_token()
        url = f"{GITHUB_API}/repos/{owner}/{name}"
        data = self._http_post(url, _headers(token), None, "GET")
        if data.get("message") and not data.get("id"):
            raise OrchestrationError(_safe_detail(str(data.get("message"))))
        return {
            "ok": True,
            "full_name": data.get("full_name"),
            "default_branch": data.get("default_branch"),
            "private": data.get("private"),
        }

    def get_release_by_tag(self, owner: str, name: str, tag: str) -> dict[str, Any] | None:
        token = self._auth_token()
        url = f"{GITHUB_API}/repos/{owner}/{name}/releases/tags/{tag}"
        data = self._http_post(url, _headers(token), None, "GET")
        if data.get("message") == "Not Found":
            return None
        if data.get("message") and not data.get("id"):
            raise OrchestrationError(_safe_detail(str(data.get("message"))))
        return data

    def create_release(
        self,
        owner: str,
        name: str,
        *,
        tag: str,
        title: str,
        body: str,
        target_commitish: str,
    ) -> dict[str, Any]:
        existing = self.get_release_by_tag(owner, name, tag)
        if existing is not None:
            return existing
        token = self._auth_token()
        url = f"{GITHUB_API}/repos/{owner}/{name}/releases"
        data = self._http_post(
            url,
            _headers(token),
            {
                "tag_name": tag,
                "target_commitish": target_commitish,
                "name": title,
                "body": body,
                "draft": False,
                "prerelease": False,
            },
        )
        if not data.get("id"):
            raise OrchestrationError(_safe_detail(str(data.get("message") or "create release failed")))
        return data

    def upload_release_asset(
        self,
        upload_url: str,
        *,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        token = self._auth_token()
        base = upload_url.split("{", 1)[0]
        url = f"{base}?name={filename}"
        if self._http_post is not default_http_post:
            return self._http_post(
                url,
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": content_type,
                },
                {"content": content},
                "POST",
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
        }
        request = urllib.request.Request(url, data=content, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
        if not data.get("id"):
            raise OrchestrationError(_safe_detail(str(data.get("message") or "asset upload failed")))
        return data

    def publish_release_assets(
        self,
        owner: str,
        name: str,
        *,
        tag: str,
        title: str,
        body: str,
        target_commitish: str,
        assets: dict[str, tuple[bytes, str]],
    ) -> PublicationResult:
        release = self.create_release(
            owner,
            name,
            tag=tag,
            title=title,
            body=body,
            target_commitish=target_commitish,
        )
        upload_url = str(release.get("upload_url") or "")
        if not upload_url:
            raise OrchestrationError("GitHub release missing upload_url")
        asset_urls: dict[str, str] = {}
        for filename, (content, content_type) in assets.items():
            uploaded = self.upload_release_asset(
                upload_url,
                filename=filename,
                content=content,
                content_type=content_type,
            )
            asset_urls[filename] = str(uploaded.get("browser_download_url") or "")
        release_url = str(release.get("html_url") or "")
        return PublicationResult(
            ok=True,
            release_url=release_url,
            tag=tag,
            asset_urls=asset_urls,
            detail="published",
        )
