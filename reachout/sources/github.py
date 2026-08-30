"""GitHub as a free contact source.

Public git commits carry the author's name and the email they committed with.
That makes the GitHub API the single best zero-cost way to (a) find a specific
person's address and (b) harvest enough real name->email pairs at a company to
learn its naming pattern. Everything here uses the documented public API.

Set GITHUB_TOKEN (any free personal access token, no scopes needed) to lift the
rate limit from 60 to 5,000 requests/hour.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from .. import config
from ..fetch import get
from ..names import parse as parse_name, slug

API = "https://api.github.com"

# GitHub's privacy relay and other addresses that are never a real work inbox.
NOISE_DOMAINS = {
    "users.noreply.github.com", "noreply.github.com", "localhost", "localhost.localdomain",
    "example.com", "example.org", "test.com", "invalid", "none.com",
}
NOISE_LOCALS = {"root", "ubuntu", "vagrant", "runner", "builder", "jenkins", "travis",
                "circleci", "actions", "noreply", "no-reply", "git", "admin"}

_BOT_RE = re.compile(
    r"(\[bot\]|-bot$|^dependabot|^renovate|^github-actions|^github-team|"
    r"-team-|^team-|-team$|^ci-|^build-|^release-)", re.I)


class RateLimited(Exception):
    pass


def _headers() -> dict:
    head = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if config.GITHUB_TOKEN:
        head["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return head


def _api(path: str, use_cache: bool = True):
    url = path if path.startswith("http") else f"{API}{path}"
    response = get(url, headers=_headers(), use_cache=use_cache, check_robots=False)
    if response.status == 403 and "rate limit" in response.text.lower():
        raise RateLimited(
            "GitHub API rate limit hit. Set GITHUB_TOKEN to raise it from 60 to 5000 req/hour."
        )
    if response.status != 200:
        return None
    import json
    try:
        return json.loads(response.text)
    except ValueError:
        return None


def rate_limit() -> dict:
    data = _api("/rate_limit", use_cache=False) or {}
    core = (data.get("resources") or {}).get("core", {})
    return {"limit": core.get("limit", 0), "remaining": core.get("remaining", 0),
            "authenticated": bool(config.GITHUB_TOKEN)}


def is_usable_email(address: str) -> bool:
    address = (address or "").strip().lower()
    if not address or "@" not in address or address.count("@") != 1:
        return False
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if domain in NOISE_DOMAINS or domain.endswith(".local") or domain.endswith(".invalid"):
        return False
    if local in NOISE_LOCALS or _BOT_RE.search(local) or _BOT_RE.search(domain):
        return False
    return True


@dataclass
class GitHubUser:
    login: str
    name: str = ""
    company: str = ""
    blog: str = ""
    email: str = ""
    location: str = ""
    bio: str = ""
    twitter: str = ""
    profile_url: str = ""
    emails: list = field(default_factory=list)  # [(address, source_url)]


def get_user(login: str) -> GitHubUser | None:
    data = _api(f"/users/{urllib.parse.quote(login)}")
    if not data or "login" not in data:
        return None
    user = GitHubUser(
        login=data["login"],
        name=data.get("name") or "",
        company=(data.get("company") or "").lstrip("@").strip(),
        blog=data.get("blog") or "",
        email=data.get("email") or "",
        location=data.get("location") or "",
        bio=data.get("bio") or "",
        twitter=data.get("twitter_username") or "",
        profile_url=data.get("html_url") or f"https://github.com/{data['login']}",
    )
    if user.email and is_usable_email(user.email):
        user.emails.append((user.email.lower(), user.profile_url))
    return user


def search_users(name: str, company: str = "", limit: int = 5) -> list[dict]:
    """Find plausible GitHub accounts for a person."""
    terms = [f'"{name}"' if " " in name else name, "in:name"]
    query = " ".join(terms)
    if company:
        query += f" {company} in:login in:name in:email"
    url = f"{API}/search/users?q={urllib.parse.quote(query)}&per_page={limit}"
    data = _api(url)
    if not data:
        return []
    return [{"login": item["login"], "profile_url": item.get("html_url", ""),
             "score": item.get("score", 0)} for item in data.get("items", [])[:limit]]


def emails_from_events(login: str, limit_events: int = 100) -> list[tuple[str, str, str]]:
    """(name, email, source_url) harvested from a user's public push events."""
    data = _api(f"/users/{urllib.parse.quote(login)}/events/public?per_page={min(limit_events,100)}")
    if not isinstance(data, list):
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for event in data:
        if event.get("type") != "PushEvent":
            continue
        repo = (event.get("repo") or {}).get("name", "")
        for commit in (event.get("payload") or {}).get("commits", []):
            author = commit.get("author") or {}
            address = (author.get("email") or "").strip().lower()
            author_name = (author.get("name") or "").strip()
            if not is_usable_email(address) or address in seen:
                continue
            seen.add(address)
            sha = commit.get("sha", "")
            source = f"https://github.com/{repo}/commit/{sha}" if repo and sha else \
                     f"https://github.com/{login}"
            out.append((author_name, address, source))
    return out


def _repo_commit_authors(full_name: str, per_page: int = 100) -> list[tuple[str, str, str]]:
    data = _api(f"/repos/{full_name}/commits?per_page={per_page}")
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        commit = (entry.get("commit") or {})
        author = commit.get("author") or {}
        address = (author.get("email") or "").strip().lower()
        author_name = (author.get("name") or "").strip()
        if not is_usable_email(address) or not author_name:
            continue
        out.append((author_name, address, entry.get("html_url") or f"https://github.com/{full_name}"))
    return out


def harvest_org(org: str, max_repos: int = 6, domain_filter: str = "") -> dict:
    """Mine a GitHub org's public commits for real name -> email pairs.

    This is how you bootstrap a company's naming pattern for free: a handful of
    repos usually yields enough @company.com committers to infer the format.
    """
    org = org.lstrip("@").strip()
    repos = _api(f"/orgs/{urllib.parse.quote(org)}/repos?sort=pushed&per_page={max_repos}")
    if not isinstance(repos, list) or not repos:
        # Fall back to treating it as a user account.
        repos = _api(f"/users/{urllib.parse.quote(org)}/repos?sort=pushed&per_page={max_repos}")
    if not isinstance(repos, list):
        return {"org": org, "pairs": [], "repos": [], "error": "org not found or rate limited"}

    pairs: dict[str, tuple[str, str, str]] = {}
    used_repos = []
    for repo in repos[:max_repos]:
        full_name = repo.get("full_name")
        if not full_name or repo.get("fork"):
            continue
        used_repos.append(full_name)
        for author_name, address, source in _repo_commit_authors(full_name):
            domain = address.split("@")[-1]
            if domain_filter and domain != domain_filter.lower():
                continue
            if address not in pairs:
                pairs[address] = (author_name, address, source)

    return {"org": org, "repos": used_repos, "pairs": list(pairs.values()), "error": ""}


def domain_hint(user: GitHubUser) -> str:
    """Best guess at the person's work domain from their GitHub profile."""
    for address, _src in user.emails:
        domain = address.split("@")[-1]
        if domain not in NOISE_DOMAINS:
            return domain
    if user.blog:
        blog = user.blog if "://" in user.blog else f"https://{user.blog}"
        host = urllib.parse.urlsplit(blog).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host and "." in host and "github." not in host:
            return host
    return ""


def match_score(user: GitHubUser, want_name: str, want_company: str = "") -> float:
    """How well a GitHub account matches the person we are looking for (0-1)."""
    score = 0.0
    target = parse_name(want_name)
    theirs = parse_name(user.name or user.login)
    if target.first and theirs.first == target.first:
        score += 0.35
    if target.last and theirs.last == target.last:
        score += 0.35
    if slug(user.login) and target.first and target.last:
        login_key = slug(user.login)
        if target.first in login_key and target.last in login_key:
            score += 0.2
        elif target.last in login_key or target.first in login_key:
            score += 0.08
    if want_company and user.company:
        if slug(want_company) and slug(want_company) in slug(user.company):
            score += 0.25
    return round(min(score, 1.0), 3)
