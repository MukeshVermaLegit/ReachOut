"""Runtime configuration. Everything is overridable by environment variable."""

from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    raw = os.environ.get("REACHOUT_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "data"


HOME = _home()


def _load_key_file() -> str:
    """Read `data/keys.env` into the environment, if it exists.

    API keys are per-machine and long-lived, and re-exporting them in every
    shell is the reason people end up running without them. A real environment
    variable always wins, so this can never override a deliberate override.

    Format is one `NAME=value` per line, `#` for comments. The file lives under
    `data/`, which is already gitignored.
    """
    path = Path(os.environ.get("REACHOUT_KEYS", HOME / "keys.env"))
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip("'\"")
            if name and name not in os.environ:
                os.environ[name] = value
    except OSError:
        return ""
    return str(path)


KEY_FILE = _load_key_file()

DB_PATH = Path(os.environ.get("REACHOUT_DB", HOME / "reachout.db"))
CACHE_DIR = Path(os.environ.get("REACHOUT_CACHE", HOME / "cache"))

# Politeness / throttling
USER_AGENT = os.environ.get(
    "REACHOUT_UA",
    "ReachOut/0.1 (personal contact research tool; +https://github.com/local/reachout)",
)
HTTP_TIMEOUT = float(os.environ.get("REACHOUT_HTTP_TIMEOUT", "12"))
HTTP_CACHE_TTL = int(os.environ.get("REACHOUT_CACHE_TTL", str(60 * 60 * 24 * 7)))
PER_HOST_DELAY = float(os.environ.get("REACHOUT_HOST_DELAY", "1.5"))
MAX_PAGE_BYTES = int(os.environ.get("REACHOUT_MAX_PAGE_BYTES", str(2_000_000)))
RESPECT_ROBOTS = os.environ.get("REACHOUT_RESPECT_ROBOTS", "1") not in ("0", "false", "no")

# DNS
DNS_TIMEOUT = float(os.environ.get("REACHOUT_DNS_TIMEOUT", "3"))
DNS_CACHE_TTL = int(os.environ.get("REACHOUT_DNS_CACHE_TTL", str(60 * 60 * 12)))

# SMTP probing is OFF unless explicitly enabled. See docs/VERIFICATION.md.
SMTP_ENABLED = os.environ.get("REACHOUT_SMTP", "0") in ("1", "true", "yes")
SMTP_FROM = os.environ.get("REACHOUT_SMTP_FROM", "verify@example.com")
SMTP_TIMEOUT = float(os.environ.get("REACHOUT_SMTP_TIMEOUT", "10"))

# Licensed-provider responses are metered and usually billed, so they are cached
# for longer than a web page - but not indefinitely: a stale number is worse than
# a slow one.
PROVIDER_CACHE_TTL = int(os.environ.get("REACHOUT_PROVIDER_CACHE_TTL", str(60 * 60 * 24 * 30)))

# Optional API keys. All optional; GOOGLE_CSE and GITHUB are free, BRAVE is
# postpaid with a monthly credit that covers roughly 1,000 requests.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "").strip()
# Serper: a Google SERP passthrough, so `site:`, `filetype:` and quoted phrases
# reach Google unchanged - which the directory and document queries depend on.
# Prepaid credit packs, so there is no billing relationship to keep open.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "").strip()
# A SearXNG instance you run yourself, e.g. http://localhost:8080. No key, no
# quota, no account; it needs `json` in its settings.yml `formats` list.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").strip().rstrip("/")
GOOGLE_CSE_KEY = os.environ.get("GOOGLE_CSE_KEY", "").strip()
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "").strip()
# Google no longer offers "Search the entire web" on every new Programmable
# Search Engine, so most are restricted to a list of sites. That is fine, but it
# changes what an empty result means: "not on those sites" is not "not on the
# web", and a restricted engine must never be allowed to end the search chain.
# Defaults to off, because assuming restricted is safe and assuming whole-web is
# not - the cost of being wrong here is a silently narrowed search.
GOOGLE_CSE_WHOLE_WEB = os.environ.get("GOOGLE_CSE_WHOLE_WEB", "0") in ("1", "true", "yes")


def ensure_dirs() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
