import time
import os
import csv
import hashlib
import secrets
import hmac
import calendar
import re
import difflib
import threading
from functools import lru_cache
import html as html_lib
import json
import asyncio
from collections import Counter, OrderedDict, defaultdict
from html.parser import HTMLParser

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Header,
    Query,
    UploadFile,
    File,
)

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import RedirectResponse, Response

from pydantic import BaseModel

from sqlalchemy import (
    create_engine,
    String,
    Text,
    Integer,
    DateTime,
    Boolean,
    select,
    or_,
    and_,
    func,
)

# Aliased: many functions in this module use local variables named text/update.
from sqlalchemy import text as sa_text, update as sa_update

from sqlalchemy.exc import IntegrityError

from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
    Session,
)


# =========================================================
# CONFIGURATION
# =========================================================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://mediaintel:mediaintel@db:5432/mediaintel",
)

ADMIN_API_KEY = os.getenv(
    "ADMIN_API_KEY",
    "change-this-admin-key",
)

FREE_SEARCH_LIMIT = int(
    os.getenv(
        "FREE_SEARCH_LIMIT",
        "4",
    )
)


# ---------------------------------------------------------
# SEARCH PERFORMANCE SETTINGS
# ---------------------------------------------------------
# Number of SQL candidates ranked in Python per search (unchanged default).
SEARCH_CANDIDATE_LIMIT = max(50, int(os.getenv("SEARCH_CANDIDATE_LIMIT", "800")))
# Articles whose normalised search text is kept in memory between searches.
SEARCH_ARTICLE_CACHE_SIZE = max(1000, int(os.getenv("SEARCH_ARTICLE_CACHE_SIZE", "20000")))
# Create pg_trgm / search indexes at startup (in a background thread).
CREATE_SEARCH_INDEXES = os.getenv("CREATE_SEARCH_INDEXES", "true").strip().lower() in {"1", "true", "yes", "on"}
# Pre-compute search text for the newest articles at startup.
SEARCH_CACHE_WARMUP = os.getenv("SEARCH_CACHE_WARMUP", "true").strip().lower() in {"1", "true", "yes", "on"}
# A publisher page that had no preview image is not fetched again for N days.
IMAGE_RECHECK_DAYS = int(os.getenv("IMAGE_RECHECK_DAYS", "7"))


class _LRUCache:
    """Small thread-safe LRU cache (sync endpoints run in a threadpool)."""

    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


AUTO_RSS_DISCOVERY_KEY = "auto_rss_discovery_enabled"
AUTO_RSS_DISCOVERY_INTERVAL_KEY = "auto_rss_discovery_interval_minutes"
AUTO_RSS_DISCOVERY_DEFAULT_INTERVAL = int(os.getenv("AUTO_RSS_DISCOVERY_INTERVAL_MINUTES", "360"))
AUTO_RSS_DISCOVERY_USER_AGENT = "AdamasMediaIntelligence/1.0 (+RSS discovery; contact site administrator)"

# =========================================================
# ePAPER AUTO-DISCOVERY
# =========================================================
# Discovery is metadata/link discovery only. The application never downloads,
# archives, or republishes ePaper PDFs/images. It stores official edition URLs
# so an administrator can open the publisher's own page.
EPAPER_DISCOVERY_USER_AGENT = (
    "AdamasMediaIntelligence/1.0 (+official ePaper link discovery)"
)
EPAPER_DISCOVERY_FILE = Path("/app/data/epaper_discovery.json")
EPAPER_MAX_RESULTS = 120

EPAPER_PUBLISHERS = [
    {
        "id": "anandabazar",
        "name": "Anandabazar Patrika",
        "url": "https://epaper.anandabazar.com/",
        "language": "Bengali",
        "coverage": "West Bengal / India",
    },
    {
        "id": "jagran",
        "name": "Dainik Jagran",
        "url": "https://epaper.jagran.com/epaper/",
        "language": "Hindi",
        "coverage": "India",
    },
    {
        "id": "indian-express-group",
        "name": "Indian Express Group ePapers",
        "url": "https://epaperarchive.indianexpress.com/",
        "language": "English / Hindi / Marathi",
        "coverage": "India",
    },
    {
        "id": "hindustan-times",
        "name": "Hindustan Times",
        "url": "https://epaper.hindustantimes.com/",
        "language": "English",
        "coverage": "India",
    },
]


def _epaper_json_read():
    try:
        if not EPAPER_DISCOVERY_FILE.exists():
            return []
        data = json.loads(EPAPER_DISCOVERY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _epaper_json_write(rows):
    EPAPER_DISCOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = EPAPER_DISCOVERY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(EPAPER_DISCOVERY_FILE)


class _EpaperLinkParser(HTMLParser):
    """Small dependency-free HTML link parser for public publisher pages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._parts = []
        self._tag_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a" and attrs.get("href"):
            self._href = attrs.get("href")
            self._parts = []
            self._tag_depth = 1
        elif self._href is not None:
            self._tag_depth += 1

    def handle_data(self, data):
        if self._href is not None and data:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if self._href is None:
            return
        if tag.lower() == "a" and self._tag_depth == 1:
            label = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            self.links.append({"href": self._href, "label": label})
            self._href = None
            self._parts = []
            self._tag_depth = 0
        else:
            self._tag_depth = max(1, self._tag_depth - 1)


def _epaper_clean_url(raw_url, base_url):
    try:
        url = urljoin(base_url, str(raw_url or "").strip())
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        # Drop fragments; preserve query strings because some edition URLs use them.
        return url.split("#", 1)[0]
    except Exception:
        return None


def _epaper_host_allowed(url, seed_url):
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        seed_host = (urlparse(seed_url).hostname or "").lower().rstrip(".")
        if not host or not seed_host:
            return False
        return host == seed_host or host.endswith("." + seed_host)
    except Exception:
        return False


def _epaper_candidate_score(url, label=""):
    u = str(url or "").lower()
    t = str(label or "").lower()
    score = 0
    strong = ("/edition/", "/editions/", "edition-", "/epaper/", "epaper/", "e-paper", "epaper")
    if any(token in u for token in strong):
        score += 7
    if re.search(r"(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])", u):
        score += 4
    if re.search(r"(?:19|20)\d{2}[-_/](?:0?[1-9]|[12]\d|3[01])[-_/](?:0?[1-9]|1[0-2])", u):
        score += 3
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", u):
        score += 2
    if any(token in t for token in ("edition", "e-paper", "epaper", "newspaper", "आज का", "संस्करण")):
        score += 3
    if any(token in u for token in ("login", "signin", "sign-in", "subscribe", "subscription", "contact-us", "terms", "privacy")):
        score -= 4
    return score


def _epaper_date_from_text(value):
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    patterns = [
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b",
        r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b",
        r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})\b",
        r"\b(\d{1,2})[- ](Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?) [- ](\d{4})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, s, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def _epaper_edition_from_link(label, url):
    label = re.sub(r"\s+", " ", str(label or "")).strip()
    if label and label.lower() not in {"image", "read now", "click here", "view", "download", "open"}:
        if len(label) <= 120:
            return label
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if parts:
        candidate = parts[-1]
        candidate = re.sub(r"\.(html?|php)$", "", candidate, flags=re.I)
        candidate = re.sub(r"(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])", "", candidate)
        candidate = re.sub(r"(?:19|20)\d{2}[-_/](?:0?[1-9]|[12]\d|3[01])[-_/](?:0?[1-9]|1[0-2])", "", candidate)
        candidate = re.sub(r"[-_]+", " ", candidate).strip()
        if candidate and candidate.lower() not in {"epaper", "edition", "editions"}:
            return candidate[:120].title()
    return "Edition"


def _epaper_language_from_html(html, fallback):
    # Lightweight language signal; do not attempt OCR or content extraction.
    sample = str(html or "")[:250000]
    if re.search(r"[\u0900-\u097F]", sample):
        return "Hindi"
    if re.search(r"[\u0980-\u09FF]", sample):
        return "Bengali"
    return fallback or "English"


def _discover_epaper_links(seed_url, publisher, language=None):
    timeout = httpx.Timeout(15.0, connect=6.0)
    headers = {
        "User-Agent": EPAPER_DISCOVERY_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(seed_url)
        response.raise_for_status()
        final_url = str(response.url)
        content_type = (response.headers.get("content-type") or "").lower()
        if "html" not in content_type and not response.text.lstrip().startswith("<"):
            raise ValueError("The supplied URL did not return an HTML page.")
        html = response.text[:4_000_000]

    parser = _EpaperLinkParser()
    parser.feed(html)
    parser.close()
    detected_language = _epaper_language_from_html(html, language or publisher.get("language"))

    rows = []
    seen = set()
    for link in parser.links:
        url = _epaper_clean_url(link.get("href"), final_url)
        if not url or url in seen or not _epaper_host_allowed(url, seed_url):
            continue
        score = _epaper_candidate_score(url, link.get("label"))
        if score < 6:
            continue
        seen.add(url)
        combined = f"{link.get('label','')} {url}"
        rows.append({
            "publisher": publisher.get("name") or "Custom Publisher",
            "publisher_id": publisher.get("id") or "custom",
            "edition": _epaper_edition_from_link(link.get("label"), url),
            "date": _epaper_date_from_text(combined),
            "language": detected_language,
            "url": url,
            "source_url": final_url,
            "score": score,
            "status": "discovered",
        })

    # Prefer stronger, date-bearing edition links and remove duplicate edition labels.
    rows.sort(key=lambda x: (x["score"], bool(x["date"]), x["edition"]), reverse=True)
    unique = []
    seen_key = set()
    for row in rows:
        key = (row["url"].lower(), row["edition"].lower())
        if key in seen_key:
            continue
        seen_key.add(key)
        unique.append(row)
        if len(unique) >= EPAPER_MAX_RESULTS:
            break
    return final_url, unique

AUTO_DISCOVERY_QUERIES = [
    "latest news India", "India education university", "India business economy",
    "India technology AI", "India science research", "India health",
    "India sports", "India politics", "world news", "Asia news",
    "West Bengal Kolkata news", "Bengali news India", "English India news",
    "Indian newspapers RSS feed", "Bengali newspaper RSS feed West Bengal",
    "India newspaper RSS feeds", "West Bengal newspaper RSS",
]

# Official publisher RSS directory/index pages. These are used as an additional
# discovery path so newspaper feeds are not missed when a publisher does not
# expose <link rel=alternate> tags on its homepage. The discovery engine still
# validates every extracted URL as a live RSS/Atom feed before storing it.
AUTO_NEWSPAPER_RSS_DIRECTORIES = [
    {"name": "ABP Ananda", "website": "https://bengali.abplive.com", "index": "https://bengali.abplive.com/rss", "language": "Bengali", "coverage": "West Bengal / India"},
    {"name": "The Indian Express", "website": "https://indianexpress.com", "index": "https://indianexpress.com/rss/", "language": "English", "coverage": "National / India"},
    {"name": "Times of India", "website": "https://timesofindia.indiatimes.com", "index": "https://timesofindia.indiatimes.com/rss.cms", "language": "English", "coverage": "National / India"},
    {"name": "Hindustan Times", "website": "https://www.hindustantimes.com", "index": "https://www.hindustantimes.com/rss", "language": "English", "coverage": "National / India"},
    {"name": "NDTV", "website": "https://www.ndtv.com", "index": "https://www.ndtv.com/rss", "language": "English", "coverage": "National / India"},
    {"name": "India Today", "website": "https://www.indiatoday.in", "index": "https://www.indiatoday.in/rss", "language": "English", "coverage": "National / India"},
    {"name": "The Economic Times", "website": "https://economictimes.indiatimes.com", "index": "https://economictimes.indiatimes.com/rss.cms", "language": "English", "coverage": "Business / India"},
    {"name": "Business Standard", "website": "https://www.business-standard.com", "index": "https://www.business-standard.com/rss-feeds/listing", "language": "English", "coverage": "Business / India"},
    {"name": "Financial Express", "website": "https://www.financialexpress.com", "index": "https://www.financialexpress.com/syndication/", "language": "English", "coverage": "Business / India"},
]


# =========================================================
# DATABASE BASE
# =========================================================

class Base(DeclarativeBase):
    pass


# =========================================================
# SOURCE TABLE
# =========================================================

class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    source_code: Mapped[str] = mapped_column(
        String(30),
        unique=True,
    )

    name: Mapped[str] = mapped_column(
        String(255)
    )

    website: Mapped[str] = mapped_column(
        String(500)
    )

    rss_url: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )

    language: Mapped[str] = mapped_column(
        String(100)
    )

    coverage: Mapped[str] = mapped_column(
        String(255)
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    decision: Mapped[str] = mapped_column(
        String(255),
        default="HOLD",
    )

    last_fetch_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )


# =========================================================
# SOURCE FEEDS TABLE
# =========================================================

class SourceFeed(Base):
    __tablename__ = "source_feeds"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    source_id: Mapped[int] = mapped_column(
        Integer,
        index=True,
    )

    feed_name: Mapped[str] = mapped_column(
        String(255)
    )

    feed_url: Mapped[str] = mapped_column(
        String(1000),
        unique=True,
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    language: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    decision: Mapped[str] = mapped_column(
        String(50),
        default="HOLD",
    )

    rss_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    last_test_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_fetch_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


# =========================================================
# ARTICLES TABLE
# =========================================================

class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    source_id: Mapped[int] = mapped_column(
        Integer,
        index=True,
    )

    title: Mapped[str] = mapped_column(
        String(1000),
        index=True,
    )

    url: Mapped[str] = mapped_column(
        String(1500),
        unique=True,
    )

    content_hash: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # Article thumbnail / news image
    image_url: Mapped[Optional[str]] = mapped_column(
        String(2000),
        nullable=True,
    )

    # When the publisher page was last checked for a preview image, so pages
    # without an image are not re-fetched on every view. Added to existing
    # databases by _ensure_schema() at startup.
    image_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    language: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


# =========================================================
# USER USAGE TABLE
# =========================================================

class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    setting_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    setting_value: Mapped[str] = mapped_column(String(500))


class Usage(Base):
    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    user_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
    )

    searches_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    subscribed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )


class SubscriberAccount(Base):
    __tablename__ = "subscriber_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
    )

    user_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
    )

    subscribed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        index=True,
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    auth_token: Mapped[Optional[str]] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )

    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# =========================================================
# MEDIA INTELLIGENCE V3 TABLES
# =========================================================

class StoryCluster(Base):
    __tablename__ = "story_clusters"
    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(1000))
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    language: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    article_count: Mapped[int] = mapped_column(Integer, default=0)

class StoryClusterArticle(Base):
    __tablename__ = "story_cluster_articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(Integer, index=True)
    article_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    similarity_score: Mapped[float] = mapped_column(default=0.0)

class TrackedTopic(Base):
    __tablename__ = "tracked_topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_key: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255))
    query: Mapped[str] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class SavedSearch(Base):
    __tablename__ = "saved_searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_key: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255))
    query: Mapped[str] = mapped_column(String(500))
    filters_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_key: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255))
    query: Mapped[str] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Briefing(Base):
    __tablename__ = "briefings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_key: Mapped[str] = mapped_column(String(255), index=True)
    briefing_date: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# =========================================================
# SEARCH QUERY HISTORY / POPULARITY
# =========================================================

class SearchQueryLog(Base):
    __tablename__ = "search_query_log"

    id: Mapped[int] = mapped_column(primary_key=True)

    query: Mapped[str] = mapped_column(String(500), index=True)

    normalized_query: Mapped[str] = mapped_column(
        String(500),
        index=True,
    )

    search_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
    )

    last_searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


# =========================================================
# =========================================================
# APPLICATION SETTINGS HELPERS
# =========================================================

FREE_SEARCH_LIMIT_KEY = "free_search_limit"

def get_free_search_limit(db: Session) -> int:
    setting = db.scalar(
        select(AppSetting).where(AppSetting.setting_key == FREE_SEARCH_LIMIT_KEY)
    )
    if setting is None:
        setting = AppSetting(
            setting_key=FREE_SEARCH_LIMIT_KEY,
            setting_value=str(FREE_SEARCH_LIMIT),
        )
        db.add(setting)
        db.commit()
        db.refresh(setting)
    try:
        value = int(setting.setting_value)
    except (TypeError, ValueError):
        value = FREE_SEARCH_LIMIT
    return max(0, value)


# =========================================================
# AUTOMATIC GLOBAL RSS DISCOVERY
# =========================================================

def get_app_bool(db: Session, key: str, default: bool = False) -> bool:
    setting = db.scalar(select(AppSetting).where(AppSetting.setting_key == key))
    if setting is None:
        return default
    return str(setting.setting_value).strip().lower() in {"1", "true", "yes", "on"}


def get_app_int(db: Session, key: str, default: int) -> int:
    setting = db.scalar(select(AppSetting).where(AppSetting.setting_key == key))
    if setting is None:
        return default
    try:
        return max(15, int(setting.setting_value))
    except (TypeError, ValueError):
        return default


def set_app_setting(db: Session, key: str, value: str) -> None:
    setting = db.scalar(select(AppSetting).where(AppSetting.setting_key == key))
    if setting is None:
        setting = AppSetting(setting_key=key, setting_value=str(value))
        db.add(setting)
    else:
        setting.setting_value = str(value)


def _safe_source_code(website: str) -> str:
    host = (urlparse(website).hostname or "source").lower()
    host = re.sub(r"^www\.", "", host)
    code = re.sub(r"[^a-z0-9]+", "_", host).strip("_").upper()
    return (code[:27] or "AUTO_SOURCE")


def _unique_source_code(db: Session, base_code: str) -> str:
    code = base_code[:30]
    if db.scalar(select(Source).where(Source.source_code == code)) is None:
        return code
    for i in range(2, 1000):
        suffix = f"_{i}"
        candidate = f"{base_code[:30-len(suffix)]}{suffix}"
        if db.scalar(select(Source).where(Source.source_code == candidate)) is None:
            return candidate
    return f"AUTO_{secrets.token_hex(6).upper()}"[:30]


def _canonical_website(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _extract_feed_links(page_url: str, body: str) -> list[str]:
    links = []
    # RSS/Atom autodiscovery is the preferred publisher signal.
    pattern = re.compile(
        r'<link\b[^>]*?rel=["\']([^"\']*?\balternate\b[^"\']*)["\'][^>]*?href=["\']([^"\']+)["\'][^>]*?>',
        re.I | re.S,
    )
    for rel, href in pattern.findall(body):
        tag_start = max(0, body.lower().rfind("<link", 0, body.lower().find(href.lower())))
        tag = body[tag_start:body.lower().find("href", tag_start) + 200] if tag_start >= 0 else ""
        if "rss+xml" in tag.lower() or "atom+xml" in tag.lower():
            links.append(urljoin(page_url, href.strip()))
    # Some sites put type before rel or use single quotes/attribute ordering.
    for match in re.finditer(r'<link\b[^>]*>', body, re.I | re.S):
        tag = match.group(0)
        if re.search(r'type\s*=\s*["\']application/(?:rss|atom)\+xml["\']', tag, re.I):
            m = re.search(r'href\s*=\s*["\']([^"\']+)', tag, re.I)
            if m:
                links.append(urljoin(page_url, m.group(1).strip()))
    return list(dict.fromkeys(links))[:10]


def _extract_directory_feed_links(page_url: str, body: str) -> list[str]:
    """Extract likely RSS/Atom URLs from publisher RSS directory pages.

    Publisher directories commonly expose feed URLs as normal <a href> links
    rather than RSS autodiscovery <link> tags. This extractor intentionally
    accepts only URLs whose path looks like an RSS/feed endpoint; every result
    is validated by _fetch_feed_candidate before persistence.
    """
    candidates: list[str] = []
    # href-based directory links
    for match in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>', body, re.I | re.S):
        href = html.unescape(match.group(1).strip())
        absolute = urljoin(page_url, href)
        path = urlparse(absolute).path.lower()
        if any(marker in path for marker in ("/rss", "/feed", "/feeds", "syndication")) or path.endswith((".xml", ".rss", ".atom", ".cms")):
            candidates.append(absolute)
    # Plain-text URLs are common on publisher feed index pages.
    for raw in re.findall(r'https?://[^\s<>&"\']+', body, re.I):
        absolute = raw.rstrip(').,;]')
        path = urlparse(absolute).path.lower()
        if any(marker in path for marker in ("/rss", "/feed", "/feeds", "syndication")) or path.endswith((".xml", ".rss", ".atom", ".cms")):
            candidates.append(absolute)
    # Keep only publisher-domain URLs and deduplicate.
    host = (urlparse(page_url).hostname or "").lower().lstrip("www.")
    output = []
    for candidate in candidates:
        parsed = urlparse(candidate)
        if not parsed.scheme or not parsed.netloc:
            continue
        cand_host = (parsed.hostname or "").lower().lstrip("www.")
        if host and cand_host != host and not cand_host.endswith("." + host):
            continue
        clean = candidate.split("#", 1)[0].rstrip("/")
        if clean not in output:
            output.append(clean)
    return output[:80]


async def _fetch_feed_candidate(client: httpx.AsyncClient, url: str) -> tuple[bool, str | None, int]:
    try:
        r = await client.get(url)
        if r.status_code >= 400:
            return False, None, r.status_code
        parsed = feedparser.parse(r.content)
        if parsed.bozo and not parsed.entries:
            return False, None, r.status_code
        if not parsed.entries:
            return False, None, r.status_code
        title = getattr(parsed.feed, "title", None) or urlparse(url).netloc
        return True, str(title)[:255], r.status_code
    except Exception:
        return False, None, 0


async def discover_newspaper_rss_directories(db: Session, client: httpx.AsyncClient) -> dict:
    """Discover and validate feeds from official newspaper/publisher RSS indexes."""
    discovered_sources = 0
    discovered_feeds = 0
    directories_checked = 0
    errors: list[str] = []

    for meta in AUTO_NEWSPAPER_RSS_DIRECTORIES:
        directories_checked += 1
        try:
            response = await client.get(meta["index"])
            if response.status_code >= 400:
                errors.append(f'{meta["name"]}: RSS directory returned HTTP {response.status_code}')
                continue

            feed_links = _extract_directory_feed_links(str(response.url), response.text)
            # If the directory itself is an RSS document, accept it directly.
            if not feed_links:
                feed_links = [str(response.url)]

            valid_feeds: list[tuple[str, str]] = []
            for feed_url in feed_links:
                ok, feed_title, _ = await _fetch_feed_candidate(client, feed_url)
                if ok:
                    valid_feeds.append((feed_url, feed_title or meta["name"]))
                if len(valid_feeds) >= 40:
                    break

            if not valid_feeds:
                continue

            website = _canonical_website(meta["website"])
            source = db.scalar(select(Source).where(func.lower(Source.website) == website.lower()))
            if source is None:
                source = Source(
                    source_code=_unique_source_code(db, _safe_source_code(website)),
                    name=meta["name"][:255],
                    website=website,
                    rss_url=valid_feeds[0][0],
                    language=meta["language"],
                    coverage=meta["coverage"],
                    active=True,
                    decision="AUTO_DISCOVERED",
                )
                db.add(source)
                db.flush()
                discovered_sources += 1
            else:
                if not source.rss_url:
                    source.rss_url = valid_feeds[0][0]
                # Preserve an existing curated source while enriching missing metadata.
                if not source.language:
                    source.language = meta["language"]
                if not source.coverage:
                    source.coverage = meta["coverage"]

            for feed_url, feed_title in valid_feeds:
                existing_feed = db.scalar(select(SourceFeed).where(func.lower(SourceFeed.feed_url) == feed_url.lower()))
                if existing_feed is None:
                    db.add(SourceFeed(
                        source_id=source.id,
                        feed_name=feed_title[:255],
                        feed_url=feed_url,
                        category="News",
                        language=meta["language"],
                        active=True,
                        decision="AUTO_DISCOVERED",
                        rss_verified=True,
                        last_test_at=datetime.now(timezone.utc),
                    ))
                    discovered_feeds += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            errors.append(f'{meta["name"]}: {exc}')

    return {
        "directories_checked": directories_checked,
        "newspaper_sources_discovered": discovered_sources,
        "newspaper_feeds_discovered": discovered_feeds,
        "errors": errors[:25],
    }


async def discover_global_rss_sources(db: Session, max_queries: int | None = None) -> dict:
    """Discover publisher RSS/Atom feeds via Google News RSS, then publisher autodiscovery.

    This deliberately stores only RSS/Atom feeds exposed by publishers; it does not scrape
    arbitrary article pages. Existing sources/feeds are deduplicated by source_code/feed_url.
    """
    discovered_sources = 0
    discovered_feeds = 0
    ingested_articles = 0
    skipped_domains = 0
    errors = []
    query_list = AUTO_DISCOVERY_QUERIES[:max_queries or len(AUTO_DISCOVERY_QUERIES)]
    seen_websites: set[str] = set()
    seen_feeds: set[str] = set()

    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": AUTO_RSS_DISCOVERY_USER_AGENT}) as client:
        newspaper_result = await discover_newspaper_rss_directories(db, client)
        discovered_sources += int(newspaper_result.get("newspaper_sources_discovered", 0))
        discovered_feeds += int(newspaper_result.get("newspaper_feeds_discovered", 0))
        errors.extend(newspaper_result.get("errors", []))

        for query in query_list:
            try:
                news_rss = "https://news.google.com/rss/search?" + __import__("urllib.parse", fromlist=["urlencode"]).urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
                response = await client.get(news_rss)
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
            except Exception as exc:
                errors.append(f"Google News discovery failed for '{query}': {exc}")
                continue

            for entry in parsed.entries[:30]:
                source_meta = getattr(entry, "source", None)
                source_href = getattr(source_meta, "href", None) if source_meta else None
                article_link = getattr(entry, "link", None)
                publisher_url = _canonical_website(source_href or article_link or "")
                if not publisher_url or publisher_url in seen_websites:
                    continue
                seen_websites.add(publisher_url)

                try:
                    # Conservative robots check for the publisher homepage.
                    robots_url = urljoin(publisher_url + "/", "/robots.txt")
                    robots = await client.get(robots_url)
                    if robots.status_code < 400:
                        robots_text = robots.text.lower()
                        if re.search(r"user-agent\s*:\s*\*[^\n]*\n(?:[^\n]*\n)*?disallow\s*:\s*/\s*(?:\n|$)", robots_text):
                            skipped_domains += 1
                            continue
                    home = await client.get(publisher_url + "/")
                    if home.status_code >= 400:
                        continue
                    feed_links = _extract_feed_links(str(home.url), home.text)
                    # Common conventional feed locations as a fallback, only on the publisher domain.
                    for path in ("/rss", "/feed", "/rss.xml", "/feed.xml", "/atom.xml"):
                        feed_links.append(urljoin(publisher_url + "/", path.lstrip("/")))
                    feed_links = list(dict.fromkeys(feed_links))[:12]

                    valid_feeds = []
                    for feed_url in feed_links:
                        canonical_feed = feed_url.split("#", 1)[0].rstrip("/")
                        if canonical_feed in seen_feeds:
                            continue
                        seen_feeds.add(canonical_feed)
                        ok, feed_title, _ = await _fetch_feed_candidate(client, canonical_feed)
                        if ok:
                            valid_feeds.append((canonical_feed, feed_title or "RSS Feed"))
                            if len(valid_feeds) >= 3:
                                break

                    if not valid_feeds:
                        continue

                    source = db.scalar(select(Source).where(func.lower(Source.website) == publisher_url.lower()))
                    if source is None:
                        source_name = urlparse(publisher_url).netloc.replace("www.", "")
                        source = Source(
                            source_code=_unique_source_code(db, _safe_source_code(publisher_url)),
                            name=source_name[:255],
                            website=publisher_url,
                            rss_url=valid_feeds[0][0],
                            language="English",
                            coverage="Global / Auto-discovered",
                            active=True,
                            decision="AUTO_DISCOVERED",
                        )
                        db.add(source)
                        db.flush()
                        discovered_sources += 1
                    elif not source.rss_url:
                        source.rss_url = valid_feeds[0][0]

                    for feed_url, feed_title in valid_feeds:
                        existing_feed = db.scalar(select(SourceFeed).where(func.lower(SourceFeed.feed_url) == feed_url.lower()))
                        if existing_feed is None:
                            db.add(SourceFeed(
                                source_id=source.id,
                                feed_name=feed_title[:255],
                                feed_url=feed_url,
                                category="News",
                                language="English",
                                active=True,
                                decision="AUTO_DISCOVERED",
                                rss_verified=True,
                                last_test_at=datetime.now(timezone.utc),
                            ))
                            discovered_feeds += 1
                    db.commit()

                except Exception as exc:
                    db.rollback()
                    errors.append(f"{publisher_url}: {exc}")

    return {
        "status": "success",
        "queries_run": len(query_list),
        "directories_checked": len(AUTO_NEWSPAPER_RSS_DIRECTORIES),
        "sources_discovered": discovered_sources,
        "feeds_discovered": discovered_feeds,
        "newspaper_sources_discovered": int(newspaper_result.get("newspaper_sources_discovered", 0)),
        "newspaper_feeds_discovered": int(newspaper_result.get("newspaper_feeds_discovered", 0)),
        "articles_ingested": ingested_articles,
        "domains_skipped_by_robots": skipped_domains,
        "errors": errors[:25],
    }



async def _ingest_feed_by_id_internal(feed_id: int, db: Session) -> dict:
    """Fetch and ingest one RSS feed safely.

    Duplicate article URLs are expected during RSS discovery.  Use a SAVEPOINT
    around each insert so one duplicate cannot abort the whole SQLAlchemy
    transaction.  PostgreSQL/SQLAlchemy require an explicit rollback after a
    failed flush; begin_nested() gives us that isolated rollback point.
    """
    feed = db.get(SourceFeed, feed_id)
    if not feed or not feed.active:
        return {"added": 0, "skipped": 0, "images_updated": 0, "duplicates": 0}

    source = db.get(Source, feed.source_id)
    if not source:
        return {"added": 0, "skipped": 0, "images_updated": 0, "duplicates": 0}

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": AUTO_RSS_DISCOVERY_USER_AGENT},
        ) as client:
            response = await client.get(feed.feed_url)
            response.raise_for_status()

        parsed = feedparser.parse(response.content)
        added = skipped = images_updated = duplicates = 0
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()

        for entry in parsed.entries:
            url = getattr(entry, "link", None)
            title = getattr(entry, "title", None)
            if not url or not title:
                skipped += 1
                continue

            url = str(url).strip()
            title = str(title).strip()
            summary = getattr(entry, "summary", None)
            image_url = extract_entry_image(entry)
            content_hash = hashlib.sha256(
                f"{title}|{summary or ''}|{url}".encode("utf-8")
            ).hexdigest()

            # A feed can contain the same URL more than once in a single
            # response.  Do not stage the same URL twice in one transaction.
            if url in seen_urls or content_hash in seen_hashes:
                skipped += 1
                duplicates += 1
                continue
            seen_urls.add(url)
            seen_hashes.add(content_hash)

            existing = db.scalar(
                select(Article).where(
                    or_(Article.url == url, Article.content_hash == content_hash)
                )
            )
            if existing:
                if not existing.image_url and image_url:
                    existing.image_url = image_url[:2000]
                    images_updated += 1
                skipped += 1
                duplicates += 1
                continue

            published = None
            if getattr(entry, "published_parsed", None):
                published = datetime.fromtimestamp(
                    calendar.timegm(entry.published_parsed),
                    tz=timezone.utc,
                )

            article = Article(
                source_id=source.id,
                title=title[:1000],
                url=url[:1500],
                content_hash=content_hash,
                image_url=image_url[:2000] if image_url else None,
                summary=summary,
                category=feed.category or "News",
                language=feed.language or source.language,
                published_at=published,
            )

            # Protect against a duplicate that appears concurrently or is
            # already present but wasn't visible at the earlier SELECT.
            try:
                with db.begin_nested():
                    db.add(article)
                    db.flush()
                added += 1
            except IntegrityError:
                skipped += 1
                duplicates += 1
                continue

        now = datetime.now(timezone.utc)
        feed.last_fetch_at = now
        feed.last_error = None
        source.last_fetch_at = now
        source.last_error = None
        db.commit()
        return {
            "added": added,
            "skipped": skipped,
            "images_updated": images_updated,
            "duplicates": duplicates,
        }
    except Exception:
        db.rollback()
        raise



async def auto_discovery_worker() -> None:
    """Background scheduler for automatic RSS discovery and ingestion."""
    while True:
        try:
            with SessionLocal() as db:
                enabled = get_app_bool(db, AUTO_RSS_DISCOVERY_KEY, False)
                interval = get_app_int(
                    db,
                    AUTO_RSS_DISCOVERY_INTERVAL_KEY,
                    AUTO_RSS_DISCOVERY_DEFAULT_INTERVAL,
                )
                if enabled:
                    await discover_global_rss_sources(db)
                    auto_feeds = db.scalars(
                        select(SourceFeed).where(
                            SourceFeed.active == True,
                            SourceFeed.decision == "AUTO_DISCOVERED",
                        )
                    ).all()
                    for feed in auto_feeds:
                        try:
                            await _ingest_feed_by_id_internal(feed.id, db)
                        except Exception as exc:
                            db.rollback()
                            feed.last_error = str(exc)[:1000]
                            db.commit()
                else:
                    interval = max(60, interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            interval = AUTO_RSS_DISCOVERY_DEFAULT_INTERVAL
        await asyncio.sleep(max(60, interval * 60))

# DATABASE CONNECTION
# =========================================================

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # Explicit pool sizing. The default (5 + 10 overflow, 30 s wait) could be
    # exhausted by image-resolver requests, making /api/search wait for a
    # free connection. pool_recycle avoids stale connections on Render.
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "10")),
    pool_recycle=1800,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():

    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


# =========================================================
# SCHEMA UPGRADE + SEARCH INDEXES (PostgreSQL)
# =========================================================
# Base.metadata.create_all() never adds columns to existing tables and never
# creates trigram indexes. Without pg_trgm GIN indexes every
# "ILIKE '%term%'" in /api/search is a sequential scan of the articles table.

SEARCH_INDEX_DDL = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_articles_title_trgm "
    "ON articles USING gin (title gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_articles_summary_trgm "
    "ON articles USING gin (summary gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_articles_category_trgm "
    "ON articles USING gin (category gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_articles_published_at_desc "
    "ON articles (published_at DESC NULLS LAST)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_search_query_log_normalized_trgm "
    "ON search_query_log USING gin (normalized_query gin_trgm_ops)",
    "ANALYZE articles",
]


def _ensure_schema() -> None:
    """Additive column upgrade; runs before the API serves requests."""
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(sa_text(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS image_checked_at TIMESTAMPTZ"
        ))


def _ensure_search_indexes() -> None:
    """Create search indexes without blocking writes (CONCURRENTLY).

    Runs in a background thread so a first-time build does not delay startup.
    If a CONCURRENTLY build is interrupted it leaves an INVALID index; find it
    with  SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
    and DROP INDEX CONCURRENTLY it so the next startup rebuilds it.
    """
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            for statement in SEARCH_INDEX_DDL:
                started = time.perf_counter()
                try:
                    conn.execute(sa_text(statement))
                    print(f"[search-index] ok in {time.perf_counter() - started:.1f}s: {statement[:90]}", flush=True)
                except Exception as exc:
                    print(f"[search-index] FAILED: {statement[:90]} -> {exc}", flush=True)
    except Exception as exc:
        print(f"[search-index] could not connect: {exc}", flush=True)


# =========================================================
# IMAGE EXTRACTION UTILITIES
# =========================================================

def clean_image_url(
    value: Optional[str],
    base_url: Optional[str] = None,
):

    if not value:
        return None

    value = str(value).strip()

    if not value:
        return None

    if value.startswith("data:"):
        return None

    try:

        if base_url:

            value = urljoin(
                base_url,
                value,
            )

    except Exception:
        pass

    if not value.startswith(
        ("http://", "https://")
    ):
        return None

    return value[:2000]


def extract_image_from_html(
    html: Optional[str],
    base_url: Optional[str] = None,
):

    if not html:
        return None

    patterns = [

        # Open Graph image
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',

        # Twitter image
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',

        # Normal HTML image
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html,
            re.IGNORECASE,
        )

        if match:

            image_url = clean_image_url(
                match.group(1),
                base_url,
            )

            if image_url:
                return image_url

    return None


def extract_entry_image(entry):

    entry_url = getattr(
        entry,
        "link",
        None,
    )

    # -----------------------------------------------------
    # 1. media:content
    # -----------------------------------------------------

    media_content = getattr(
        entry,
        "media_content",
        None,
    )

    if media_content:

        for item in media_content:

            if isinstance(
                item,
                dict,
            ):

                url = (
                    item.get("url")
                    or item.get("href")
                )

                image_url = clean_image_url(
                    url,
                    entry_url,
                )

                if image_url:
                    return image_url

    # -----------------------------------------------------
    # 2. media:thumbnail
    # -----------------------------------------------------

    media_thumbnail = getattr(
        entry,
        "media_thumbnail",
        None,
    )

    if media_thumbnail:

        for item in media_thumbnail:

            if isinstance(
                item,
                dict,
            ):

                url = (
                    item.get("url")
                    or item.get("href")
                )

                image_url = clean_image_url(
                    url,
                    entry_url,
                )

                if image_url:
                    return image_url

    # -----------------------------------------------------
    # 3. RSS enclosure
    # -----------------------------------------------------

    enclosures = getattr(
        entry,
        "enclosures",
        None,
    )

    if enclosures:

        for enclosure in enclosures:

            if not isinstance(
                enclosure,
                dict,
            ):
                continue

            url = (
                enclosure.get("href")
                or enclosure.get("url")
            )

            mime_type = (
                enclosure.get("type")
                or ""
            ).lower()

            if (

                url

                and (

                    not mime_type

                    or mime_type.startswith(
                        "image/"
                    )

                )

            ):

                image_url = clean_image_url(
                    url,
                    entry_url,
                )

                if image_url:
                    return image_url

    # -----------------------------------------------------
    # 4. Image field
    # -----------------------------------------------------

    image = getattr(
        entry,
        "image",
        None,
    )

    if isinstance(
        image,
        dict,
    ):

        url = (
            image.get("href")
            or image.get("url")
        )

        image_url = clean_image_url(
            url,
            entry_url,
        )

        if image_url:
            return image_url

    # -----------------------------------------------------
    # 5. Summary HTML
    # -----------------------------------------------------

    summary = getattr(
        entry,
        "summary",
        None,
    )

    image_url = extract_image_from_html(
        summary,
        entry_url,
    )

    if image_url:
        return image_url

    # -----------------------------------------------------
    # 6. Content HTML
    # -----------------------------------------------------

    contents = getattr(
        entry,
        "content",
        None,
    )

    if contents:

        for content in contents:

            if isinstance(
                content,
                dict,
            ):

                html = content.get(
                    "value"
                )

                image_url = extract_image_from_html(
                    html,
                    entry_url,
                )

                if image_url:
                    return image_url

    return None


# =========================================================
# ARTICLE PAGE IMAGE EXTRACTION
# =========================================================

def extract_image_from_article_url(
    article_url: Optional[str],
):

    """
    Fetch an article page and extract its primary preview image.
    This is used as a fallback for already-indexed articles whose
    RSS entry did not provide an image.
    """

    if not article_url:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
    }

    try:

        with httpx.Client(
            headers=headers,
            timeout=10.0,
            follow_redirects=True,
        ) as client:

            response = client.get(
                article_url
            )

            response.raise_for_status()

            html = response.text

            final_url = str(
                response.url
            )

    except Exception:

        return None

    # Prefer Open Graph / social preview images.
    image_url = extract_image_from_html(
        html,
        final_url,
    )

    if image_url:
        return image_url

    # Additional metadata variants commonly used by publishers.
    patterns = [

        r'<meta[^>]+property=["\']og:image:url["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image:url["\']',

        r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+itemprop=["\']image["\']',

        r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',

        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']image_src["\']',

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html,
            re.IGNORECASE,
        )

        if match:

            image_url = clean_image_url(
                match.group(1),
                final_url,
            )

            if image_url:
                return image_url

    return None


# =========================================================
# REQUEST MODELS
# =========================================================

class SourceIn(BaseModel):

    source_code: str

    name: str

    website: str

    rss_url: Optional[str] = None

    language: str = "English"

    coverage: str = "India"

    active: bool = False

    decision: str = "HOLD"


class FeedIn(BaseModel):

    source_id: int

    feed_name: str

    feed_url: str

    category: Optional[str] = None

    language: Optional[str] = "English"

    active: bool = False

    decision: str = "HOLD"


class EpaperDiscoverRequest(BaseModel):

    publisher_id: Optional[str] = None

    publisher: Optional[str] = None

    url: str

    language: Optional[str] = None


class EpaperSaveRequest(BaseModel):

    publisher: str

    edition: str

    date: Optional[str] = None

    language: Optional[str] = None

    url: str

    source_url: Optional[str] = None


# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(
    title="Adamas Digital Media Intelligence API",
    version="0.7.0",
)


# =========================================================
# CORS
# =========================================================

app.add_middleware(

    CORSMiddleware,

    # The frontend sends the X-User-Key header. Credentials/cookies are not
    # used by this application, so keep credentials disabled while allowing
    # the browser to make cross-origin API requests from the local frontend.
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],

)


# =========================================================
# STARTUP
# =========================================================

auto_discovery_task = None
manual_discovery_task = None
manual_discovery_status = {
    "running": False,
    "stage": "idle",
    "progress": 0,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}

async def _run_discovery_job():
    """Run discovery outside the HTTP request so long discovery cannot time out."""
    global manual_discovery_status
    manual_discovery_status.update({
        "running": True,
        "stage": "starting",
        "progress": 5,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "result": None,
        "error": None,
    })
    try:
        with SessionLocal() as db:
            manual_discovery_status.update({"stage": "discovering", "progress": 20})
            result = await discover_global_rss_sources(db)
            manual_discovery_status.update({"stage": "ingesting", "progress": 65})
            total_added = 0
            auto_feeds = db.scalars(
                select(SourceFeed).where(
                    SourceFeed.active == True,
                    SourceFeed.decision == "AUTO_DISCOVERED"
                )
            ).all()
            total_feeds = max(1, len(auto_feeds))
            for idx, feed in enumerate(auto_feeds, 1):
                try:
                    ingest_result = await _ingest_feed_by_id_internal(feed.id, db)
                    total_added += int(ingest_result.get("added", 0))
                except Exception as exc:
                    feed.last_error = str(exc)[:1000]
                    db.commit()
                manual_discovery_status["progress"] = min(98, 65 + int((idx / total_feeds) * 33))
            result["articles_ingested"] = total_added
            manual_discovery_status.update({
                "running": False,
                "stage": "completed",
                "progress": 100,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
                "error": None,
            })
    except asyncio.CancelledError:
        manual_discovery_status.update({"running": False, "stage": "cancelled", "finished_at": datetime.now(timezone.utc).isoformat()})
        raise
    except Exception as exc:
        manual_discovery_status.update({
            "running": False,
            "stage": "failed",
            "progress": 100,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc)[:2000],
        })

@app.on_event("startup")
def startup():

    # PostgreSQL may be running as a Docker container but not yet accepting
    # connections when the backend starts. Retry the initial DB connection
    # instead of allowing FastAPI/Uvicorn startup to fail permanently.
    last_error = None
    for attempt in range(1, 13):
        try:
            Base.metadata.create_all(engine)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            engine.dispose()
            if attempt < 12:
                import time
                time.sleep(2)

    if last_error is not None:
        raise last_error

    # Additive column upgrade (image_checked_at) before serving requests.
    _ensure_schema()

    # Trigram / search indexes are built in the background.
    if CREATE_SEARCH_INDEXES:
        threading.Thread(
            target=_ensure_search_indexes,
            name="search-index-builder",
            daemon=True,
        ).start()

    # Pre-compute search text for recent articles (background, best effort).
    if SEARCH_CACHE_WARMUP:
        threading.Thread(
            target=_warm_search_cache,
            name="search-cache-warmup",
            daemon=True,
        ).start()

    with SessionLocal() as db:

        source_count = db.scalar(

            select(func.count())

            .select_from(
                Source
            )

        )

        if source_count == 0:

            path = Path(
                "/app/data/source_master.csv"
            )

            if path.exists():

                with path.open(
                    encoding="utf-8",
                ) as file:

                    for row in csv.DictReader(
                        file
                    ):

                        db.add(

                            Source(

                                source_code=
                                row["source_code"],

                                name=
                                row["name"],

                                website=
                                row["website"],

                                rss_url=
                                row.get("rss_url")
                                or None,

                                language=
                                row.get("language")
                                or "English",

                                coverage=
                                row.get("coverage")
                                or "India",

                                active=(

                                    row.get(
                                        "active",
                                        "false",
                                    ).lower()

                                    == "true"

                                ),

                                decision=
                                row.get("decision")
                                or "HOLD",

                            )

                        )

                db.commit()

        global auto_discovery_task
        if auto_discovery_task is None or auto_discovery_task.done():
            auto_discovery_task = asyncio.create_task(auto_discovery_worker())



# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():

    return {

        "status":
        "ok",

        "service":
        "media-intelligence-api",

        "version":
        "0.5.0",

    }



# =========================================================
# SUBSCRIBER AUTHENTICATION
# =========================================================

SUBSCRIBER_TOKEN_DAYS = 30


def _password_hash(password: str, salt_hex: Optional[str] = None) -> str:
    password = str(password or "")
    if not password:
        raise ValueError("Password is required.")
    salt = (
        bytes.fromhex(salt_hex)
        if salt_hex
        else secrets.token_bytes(16)
    )
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        310_000,
    )
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def _password_verify(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        ).hex()
        return hmac.compare_digest(candidate, digest_hex)
    except Exception:
        return False


def _subscriber_from_token(
    auth_token: Optional[str],
    db: Session,
) -> Optional[SubscriberAccount]:
    token = str(auth_token or "").strip()
    if not token:
        return None

    account = db.scalar(
        select(SubscriberAccount).where(
            SubscriberAccount.auth_token == token
        )
    )

    if not account:
        return None

    if not account.active or not account.subscribed:
        return None

    if (
        account.token_expires_at
        and account.token_expires_at < datetime.now(timezone.utc)
    ):
        return None

    return account


class SubscriberLoginRequest(BaseModel):
    email: str
    password: str


class AdminSubscriberCreateRequest(BaseModel):
    email: str
    password: str
    subscribed: bool = True
    active: bool = True


class AdminSubscriptionUpdateRequest(BaseModel):
    subscribed: bool
    active: Optional[bool] = None


@app.post("/api/auth/login")
def subscriber_login(
    payload: SubscriberLoginRequest,
    db: Session = Depends(get_db),
):
    email = payload.email.strip().lower()

    if not email or not payload.password:
        raise HTTPException(
            status_code=400,
            detail="Email and password are required.",
        )

    account = db.scalar(
        select(SubscriberAccount).where(
            SubscriberAccount.email == email
        )
    )

    if (
        not account
        or not account.active
        or not account.subscribed
        or not _password_verify(
            payload.password,
            account.password_hash,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="Subscription access is not active. Sign In is available only to paid subscribers whose subscription has been activated by Admin.",
        )

    token = secrets.token_urlsafe(48)
    account.auth_token = token
    account.token_expires_at = (
        datetime.now(timezone.utc)
        + timedelta(days=SUBSCRIBER_TOKEN_DAYS)
    )
    account.last_login_at = datetime.now(timezone.utc)

    usage = db.scalar(
        select(Usage).where(
            Usage.user_key == account.user_key
        )
    )
    if not usage:
        usage = Usage(
            user_key=account.user_key,
            searches_used=0,
            subscribed=False,
        )
        db.add(usage)
    else:
        # Premium access is determined only by the active subscriber token.
        # Keep the legacy Usage flag false so logout cannot leave a stale
        # "unlimited" state behind.
        usage.subscribed = False

    db.commit()

    return {
        "status": "success",
        "message": "Subscriber authenticated successfully.",
        "email": account.email,
        "user_key": account.user_key,
        "token": token,
        "subscribed": True,
        "expires_at": account.token_expires_at,
    }


@app.post("/api/auth/logout")
def subscriber_logout(
    x_auth_token: Optional[str] = Header(
        None,
        alias="X-Auth-Token",
    ),
    db: Session = Depends(get_db),
):
    account = _subscriber_from_token(x_auth_token, db)
    if account:
        account.auth_token = None
        account.token_expires_at = None

        # Keep the legacy Usage.subscribed flag false. Premium access is
        # granted only while a valid subscriber token is present. This prevents
        # a previous paid session from leaving an unlimited state behind after
        # logout.
        usage = db.scalar(
            select(Usage).where(Usage.user_key == account.user_key)
        )
        if usage:
            usage.subscribed = False

        db.commit()

    return {
        "status": "success",
        "message": "Subscriber session ended.",
    }


# =========================================================
# ADMIN AUTHENTICATION
# =========================================================

def require_admin(

    x_admin_key: str = Header(
        alias="X-Admin-Key",
    )

):

    if x_admin_key != ADMIN_API_KEY:

        raise HTTPException(

            status_code=401,

            detail=
            "Invalid admin key",

        )


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.post("/api/admin/login")
def admin_login(

    x_admin_key: str = Header(
        alias="X-Admin-Key",
    )

):

    if x_admin_key != ADMIN_API_KEY:

        raise HTTPException(

            status_code=401,

            detail=
            "Invalid admin key",

        )

    return {

        "status":
        "success",

        "message":
        "Admin authenticated successfully.",

    }


# =========================================================
# USER QUOTA
# =========================================================

@app.get("/api/quota")
def quota(

    user_key: str = Header(
        alias="X-User-Key",
    ),

    x_auth_token: Optional[str] = Header(
        None,
        alias="X-Auth-Token",
    ),

    db: Session = Depends(
        get_db
    ),

):

    subscriber = _subscriber_from_token(
        x_auth_token,
        db,
    )

    effective_user_key = (
        subscriber.user_key
        if subscriber
        else user_key
    )

    usage = db.scalar(
        select(Usage).where(
            Usage.user_key == effective_user_key
        )
    )

    if not usage:

        usage = Usage(

            user_key=user_key,

            searches_used=0,

            subscribed=False,

        )

        db.add(usage)

        db.commit()

        db.refresh(usage)

    remaining = max(

        0,

        get_free_search_limit(db)
        - usage.searches_used,

    )

    # IMPORTANT: subscription state comes from the authenticated subscriber
    # token, not Usage.subscribed. Usage.subscribed is a legacy field and can
    # remain True after logout if it was set during an older login.
    is_subscriber = bool(subscriber)

    return {

        "status":
        "success",

        "user_key":
        usage.user_key,

        "free_limit":
        get_free_search_limit(db),

        "searches_used":
        usage.searches_used,

        "remaining":

        None

        if is_subscriber

        else remaining,

        "subscribed":
        is_subscriber,

    }


# =========================================================
# RESET CURRENT USER SEARCH QUOTA
# =========================================================

@app.post("/api/reset-search")
def reset_search_quota(

    user_key: str = Header(
        alias="X-User-Key",
    ),

    db: Session = Depends(
        get_db
    ),

):

    usage = db.scalar(

        select(Usage)

        .where(

            Usage.user_key
            == user_key

        )

    )

    if not usage:

        usage = Usage(

            user_key=user_key,

            searches_used=0,

            subscribed=False,

        )

        db.add(usage)

    else:

        usage.searches_used = 0

    db.commit()

    db.refresh(usage)

    return {

        "status":
        "success",

        "message":
        "Search quota has been reset successfully.",

        "user_key":
        usage.user_key,

        "searches_used":
        usage.searches_used,

        "free_limit":
        get_free_search_limit(db),

        "remaining":
        get_free_search_limit(db),

        "subscribed":
        usage.subscribed,

    }


# =========================================================
# ARTICLE IMAGE RESOLVER
# =========================================================

# Article ids whose publisher page is being fetched right now.
_image_fetch_inflight: set[int] = set()
_image_fetch_lock = threading.Lock()


@app.get("/api/articles/{article_id}/image")
def resolve_article_image(
    article_id: int,
):

    """
    Returns the article's image. Existing image URLs are reused.
    If an older article has no stored image, the publisher page is
    checked, the result is remembered (image_url on success,
    image_checked_at in both cases), and the browser is redirected.

    PERFORMANCE: the database connection is no longer held during the
    publisher fetch (up to 10 s each). Previously a results page full of
    image-less articles pinned most of the connection pool, so the next
    /api/search waited for a free connection.
    """

    with SessionLocal() as db:

        article = db.get(
            Article,
            article_id,
        )

        if not article:

            raise HTTPException(
                status_code=404,
                detail="Article not found.",
            )

        page_url = article.url

        image_url = clean_image_url(
            article.image_url,
            article.url,
        )

        checked_at = article.image_checked_at

    if image_url:

        return RedirectResponse(
            url=image_url,
            status_code=302,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    now = datetime.now(timezone.utc)

    if checked_at is not None:
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if now - checked_at < timedelta(days=IMAGE_RECHECK_DAYS):
            raise HTTPException(
                status_code=404,
                detail="Article image not available.",
                headers={"Cache-Control": "public, max-age=3600"},
            )

    with _image_fetch_lock:
        if article_id in _image_fetch_inflight:
            raise HTTPException(
                status_code=404,
                detail="Article image lookup in progress.",
            )
        _image_fetch_inflight.add(article_id)

    try:
        # Network call with no database connection held.
        found = extract_image_from_article_url(page_url)
    finally:
        with _image_fetch_lock:
            _image_fetch_inflight.discard(article_id)

    values = {"image_checked_at": now}
    if found:
        values["image_url"] = found[:2000]

    with SessionLocal() as db:
        db.execute(
            sa_update(Article)
            .where(Article.id == article_id)
            .values(**values)
        )
        db.commit()

    if not found:

        raise HTTPException(
            status_code=404,
            detail="Article image not available.",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return RedirectResponse(
        url=found,
        status_code=302,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# =========================================================
# PUBLIC SEARCH / AUTOCOMPLETE
# =========================================================

SEARCH_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for",
    "from", "in", "into", "is", "of", "on", "or", "the", "to",
    "with", "was", "were", "what", "when", "where", "who", "why",
}

# Precompiled patterns (identical semantics to the previous inline re.sub calls).
_RE_SEARCH_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
_RE_SEARCH_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.I | re.S)
_RE_SEARCH_TAG = re.compile(r"<[^>]+>")
_RE_SEARCH_URL = re.compile(r"https?://\S+")
_RE_SEARCH_NBSP = re.compile(r"[\u00a0\u200b]+")
_RE_SEARCH_WS = re.compile(r"\s+")
_RE_SEARCH_NON_WORD_RUN = re.compile(r"[^a-z0-9\u0900-\u097f\u0980-\u09ff]+")
_RE_SEARCH_NON_WORD_CHAR = re.compile(r"[^a-z0-9\u0900-\u097f\u0980-\u09ff]")


def _clean_search_text(value: Optional[str]) -> str:
    """Convert publisher HTML/entities into searchable plain text."""
    if not value:
        return ""
    value = html_lib.unescape(value)
    value = _RE_SEARCH_SCRIPT.sub(" ", value)
    value = _RE_SEARCH_STYLE.sub(" ", value)
    value = _RE_SEARCH_TAG.sub(" ", value)
    value = _RE_SEARCH_URL.sub(" ", value)
    value = _RE_SEARCH_NBSP.sub(" ", value)
    return _RE_SEARCH_WS.sub(" ", value).strip()


def _normalise_from_clean(clean: str) -> str:
    value = _RE_SEARCH_NON_WORD_RUN.sub(" ", clean.lower())
    return _RE_SEARCH_WS.sub(" ", value).strip()


def _compact_from_clean(clean: str) -> str:
    return _RE_SEARCH_NON_WORD_CHAR.sub("", clean.lower())


def _normalise_search(value: Optional[str]) -> str:
    return _normalise_from_clean(_clean_search_text(value))


# Short, highly repetitive strings (source names, categories).
_normalise_short = lru_cache(maxsize=4096)(_normalise_search)


def _compact_search(value: Optional[str]) -> str:
    """Normalize identifiers/acronyms without separators: IIT-B -> iitb."""
    return _compact_from_clean(_clean_search_text(value))


@lru_cache(maxsize=8192)
def _token_variants(token: str) -> frozenset:
    """Small morphology helper so application/applications etc. match."""
    variants = {token}
    if len(token) > 4:
        if token.endswith("ies"):
            variants.add(token[:-3] + "y")
        if token.endswith("es"):
            variants.add(token[:-2])
        if token.endswith("s"):
            variants.add(token[:-1])
        else:
            variants.add(token + "s")
    return frozenset(variants)


# =========================================================
# SEARCH ENTITY / INSTITUTION NORMALISATION
# =========================================================

# Canonical aliases for common Indian higher-education institutions.
# These are used only to prevent campus identifiers such as IIT-B and IIT-M
# from being treated as interchangeable because they share the token "IIT".
IIT_CAMPUS_ALIASES = {
    "b": [
        "iit b", "iit-b", "iitb", "iit bombay", "iit-bombay",
        "indian institute of technology bombay",
    ],
    "m": [
        "iit m", "iit-m", "iitm", "iit madras", "iit-madras",
        "indian institute of technology madras",
    ],
    "d": [
        "iit d", "iit-d", "iitd", "iit delhi", "iit-delhi",
        "indian institute of technology delhi",
    ],
    "k": [
        "iit k", "iit-k", "iitk", "iit kanpur", "iit-kanpur",
        "indian institute of technology kanpur",
    ],
    "kgp": [
        "iit kgp", "iit-kgp", "iitkgp", "iit kharagpur", "iit-kharagpur",
        "indian institute of technology kharagpur",
    ],
    "r": [
        "iit r", "iit-r", "iitr", "iit roorkee", "iit-roorkee",
        "indian institute of technology roorkee",
    ],
    "g": [
        "iit g", "iit-g", "iitg", "iit guwahati", "iit-guwahati",
        "indian institute of technology guwahati",
    ],
    "h": [
        "iit h", "iit-h", "iith", "iit hyderabad", "iit-hyderabad",
        "indian institute of technology hyderabad",
    ],
    "j": [
        "iit j", "iit-j", "iitj", "iit jodhpur", "iit-jodhpur",
        "indian institute of technology jodhpur",
    ],
    "bhu": [
        "iit bhu", "iit-bhu", "iitbhu", "iit banaras", "iit-banaras",
        "indian institute of technology bhu",
    ],
    "bbs": [
        "iit bbs", "iit-bbs", "iitbbs", "iit bhubaneswar", "iit-bhubaneswar",
        "indian institute of technology bhubaneswar",
    ],
    "patna": [
        "iit patna", "iit-patna", "iitpatna",
        "indian institute of technology patna",
    ],
    "ropar": [
        "iit ropar", "iit-ropar", "iitropar",
        "indian institute of technology ropar",
    ],
    "indore": [
        "iit indore", "iit-indore", "iitindore",
        "indian institute of technology indore",
    ],
    "mandi": [
        "iit mandi", "iit-mandi", "iitmandi",
        "indian institute of technology mandi",
    ],
    "jammu": [
        "iit jammu", "iit-jammu", "iitjammu",
        "indian institute of technology jammu",
    ],
    "tirupati": [
        "iit tirupati", "iit-tirupati", "iittirupati",
        "indian institute of technology tirupati",
    ],
    "goa": [
        "iit goa", "iit-goa", "iitgoa",
        "indian institute of technology goa",
    ],
    "palakkad": [
        "iit palakkad", "iit-palakkad", "iitpalakkad",
        "indian institute of technology palakkad",
    ],
    "dharwad": [
        "iit dharwad", "iit-dharwad", "iitdharwad",
        "indian institute of technology dharwad",
    ],
}


# =========================================================
# GENERIC HIGHER-EDUCATION ENTITY NORMALISATION
# =========================================================
# The search engine keeps institution identity separate from generic words.
# This prevents queries such as IIM-A / IIM Ahmedabad from being confused
# with another IIM campus merely because all of them contain "IIM".
INSTITUTION_CAMPUS_ALIASES = {
    "iim_a": [
        "iim a", "iim-a", "iima", "iim ahmedabad", "iim-ahmedabad",
        "indian institute of management ahmedabad",
    ],
    "iim_b": [
        "iim b", "iim-b", "iimb", "iim bangalore", "iim-bangalore",
        "indian institute of management bangalore",
    ],
    "iim_c": [
        "iim c", "iim-c", "iimc", "iim calcutta", "iim-calcutta",
        "iim kolkata", "iim-kolkata",
        "indian institute of management calcutta",
        "indian institute of management kolkata",
    ],
    "iim_l": [
        "iim l", "iim-l", "iiml", "iim lucknow", "iim-lucknow",
        "indian institute of management lucknow",
    ],
    "iim_k": [
        "iim k", "iim-k", "iimk", "iim kozhikode", "iim-kozhikode",
        "iim calicut", "iim-calicut",
        "indian institute of management kozhikode",
        "indian institute of management calicut",
    ],
    "iim_i": [
        "iim i", "iim-i", "iimi", "iim indore", "iim-indore",
        "indian institute of management indore",
    ],
    "iim_r": [
        "iim r", "iim-r", "iimr", "iim rohtak", "iim-rohtak",
        "indian institute of management rohtak",
    ],
}


# Aliases are normalised ONCE at import time. Previously every alias was
# re-normalised (several regex passes) for every candidate article.
def _alias_table(alias_map: dict) -> tuple:
    return tuple(
        (code, tuple((_normalise_search(a), _compact_search(a)) for a in aliases))
        for code, aliases in alias_map.items()
    )


_IIT_ALIAS_TABLE = _alias_table(IIT_CAMPUS_ALIASES)
_IIM_ALIAS_TABLE = _alias_table(INSTITUTION_CAMPUS_ALIASES)


def _iit_codes_prepared(normalized: str, compact: str) -> set[str]:
    if not normalized and not compact:
        return set()
    found = set()
    for code, pairs in _IIT_ALIAS_TABLE:
        for alias_norm, alias_compact in pairs:
            if alias_norm and alias_norm in normalized:
                found.add(code)
                break
            if alias_compact and alias_compact in compact:
                found.add(code)
                break
    # Also support compact shorthand forms such as IITB/IITM/IITD.
    for code in ("b", "m", "d", "k", "g", "h", "j", "r"):
        if f"iit{code}" in compact:
            found.add(code)
    return found


def _iim_codes_prepared(normalized: str, compact: str) -> set[str]:
    if not normalized and not compact:
        return set()
    found = set()
    for code, pairs in _IIM_ALIAS_TABLE:
        for alias_norm, alias_compact in pairs:
            if alias_norm in normalized or alias_compact in compact:
                found.add(code)
                break
    return found


def _entity_codes_prepared(normalized: str, compact: str) -> set[str]:
    return {
        *(f"iit:{code}" for code in _iit_codes_prepared(normalized, compact)),
        *(f"iim:{code}" for code in _iim_codes_prepared(normalized, compact)),
    }


def _iit_campus_codes(value: str) -> set[str]:
    """Return canonical IIT campus codes represented by a query or article text."""
    return _iit_codes_prepared(_normalise_search(value), _compact_search(value))


def _institution_campus_codes(value: str) -> set[str]:
    """Return canonical IIM campus identifiers represented by text."""
    return _iim_codes_prepared(_normalise_search(value), _compact_search(value))


def _search_entity_codes(value: str) -> set[str]:
    """Return all campus-specific higher-education entity codes."""
    return _entity_codes_prepared(_normalise_search(value), _compact_search(value))


# =========================================================
# PER-ARTICLE NORMALISED SEARCH TEXT (CACHED)
# =========================================================
# The ranker used to strip HTML from every candidate's summary ~8 times per
# search, for up to 800 candidates, on every request. The cleaned forms only
# depend on the article text, so they are computed once and reused.

class _ArticleSearchFields:
    __slots__ = (
        "title",
        "summary",
        "title_compact",
        "summary_compact",
        "title_words",
        "summary_words",
        "title_tokens",
        "_entity_codes",
    )

    def __init__(self, title_raw: str, summary_raw: str):
        title_clean = _clean_search_text(title_raw)
        summary_clean = _clean_search_text(summary_raw)
        self.title = _normalise_from_clean(title_clean)
        self.summary = _normalise_from_clean(summary_clean)
        self.title_compact = _compact_from_clean(title_clean)
        self.summary_compact = _compact_from_clean(summary_clean)
        self.title_tokens = tuple(self.title.split())
        self.title_words = frozenset(self.title_tokens)
        self.summary_words = frozenset(self.summary.split())
        self._entity_codes = None

    @property
    def entity_codes(self) -> set[str]:
        # Equivalent to _search_entity_codes(f"{title} {summary}").
        if self._entity_codes is None:
            combined = " ".join(part for part in (self.title, self.summary) if part)
            self._entity_codes = _entity_codes_prepared(
                combined,
                self.title_compact + self.summary_compact,
            )
        return self._entity_codes


_ARTICLE_FIELDS_CACHE = _LRUCache(SEARCH_ARTICLE_CACHE_SIZE)


def _article_fields(article: Article) -> _ArticleSearchFields:
    title = article.title or ""
    summary = article.summary or ""
    # The text hash is part of the key, so an edited article is re-processed.
    key = (article.id, hash(title), hash(summary))
    fields = _ARTICLE_FIELDS_CACHE.get(key)
    if fields is None:
        fields = _ArticleSearchFields(title, summary)
        _ARTICLE_FIELDS_CACHE.set(key, fields)
    return fields


def _warm_search_cache() -> None:
    """Pre-compute normalised search text for the newest articles.

    Runs once in a background thread at startup so the first searches after a
    Render deploy/restart do not pay the one-time HTML-cleaning cost.
    """
    try:
        started = time.perf_counter()
        with SessionLocal() as db:
            rows = db.execute(
                select(Article.id, Article.title, Article.summary)
                .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
                .limit(SEARCH_ARTICLE_CACHE_SIZE)
            ).all()
        # Oldest first, so the newest articles end up most-recently-used in the LRU.
        for index, (article_id, title, summary) in enumerate(reversed(rows), 1):
            title = title or ""
            summary = summary or ""
            key = (article_id, hash(title), hash(summary))
            if _ARTICLE_FIELDS_CACHE.get(key) is None:
                _ARTICLE_FIELDS_CACHE.set(key, _ArticleSearchFields(title, summary))
            if index % 200 == 0:
                time.sleep(0.005)  # yield the GIL to request threads
        print(f"[search-cache] warmed {len(rows)} articles in {time.perf_counter() - started:.1f}s", flush=True)
    except Exception as exc:
        print(f"[search-cache] warm-up skipped: {exc}", flush=True)


EDUCATION_FILTER_TERMS = [
    "%education%",
    "%educational%",
    "%university%",
    "%universities%",
    "%college%",
    "%colleges%",
    "%school%",
    "%schools%",
    "%academic%",
    "%academia%",
    "%student%",
    "%students%",
    "%campus%",
    "%admission%",
    "%admissions%",
    "%teaching%",
    "%learning%",
    "%higher education%",
    "%institute%",
    "%institutes%",
    "%iit%",
    "%iim%",
    "%nit%",
]


def _education_match_conditions():
    """Build a broad education filter across category, title and summary."""
    conditions = []
    for pattern in EDUCATION_FILTER_TERMS:
        conditions.extend([
            Article.title.ilike(pattern),
            Article.summary.ilike(pattern),
        ])
    return conditions


def _query_tokens(query: str) -> list[str]:
    tokens = _normalise_search(query).split()
    # Keep meaningful words; retain all words if everything is a stop word.
    meaningful = [t for t in tokens if t not in SEARCH_STOP_WORDS]
    return meaningful or tokens


def _token_present(token: str, text: str) -> bool:
    words = set(text.split())
    return bool(words.intersection(_token_variants(token)))



# =========================================================
# HIGH-QUALITY SEARCH CONCEPTS / ALIASES
# =========================================================
# These aliases improve recall without making the ranking fuzzy.
# Exact institution/campus identity remains protected by the entity gate.

# ---------------------------------------------------------------------------
# BINGLISH / BANGLA PHONETIC SEARCH
# ---------------------------------------------------------------------------
# Lets users search Bengali news using Latin/English keyboard spelling:
# "ami tumi bhat khabo", "patropatri", "chele meye", etc.
_BENGLISH_PHRASE_ALIASES = {
    "ami":["আমি"], "aami":["আমি"], "amra":["আমরা"],
    "tumi":["তুমি"], "tomra":["তোমরা"], "apni":["আপনি"],
    "bhat":["ভাত"], "bhaat":["ভাত"],
    "khabo":["খাবো","খাব"], "khab":["খাব"], "khao":["খাও"],
    "khabe":["খাবে"], "kheye":["খেয়ে","খেয়ে"],
    "patropatri":["পাত্রপাত্রী"], "patro patri":["পাত্রপাত্রী"],
    "patro":["পাত্র"], "patri":["পাত্রী"],
    "chele":["ছেলে"], "meye":["মেয়ে","মেয়ে"],
    "chele meye":["ছেলে মেয়ে","ছেলে মেয়ে"],
    "chelemeye":["ছেলেমেয়ে","ছেলেমেয়ে"],
    "biye":["বিয়ে","বিয়ে"], "bibaho":["বিবাহ"],
    "bhalobasha":["ভালোবাসা"], "bhalobasa":["ভালোবাসা"],
    "poribar":["পরিবার"], "shikkha":["শিক্ষা"],
    "school":["স্কুল"], "college":["কলেজ"],
    "biswobidyaloy":["বিশ্ববিদ্যালয়","বিশ্ববিদ্যালয়"],
    "bishwabidyaloy":["বিশ্ববিদ্যালয়","বিশ্ববিদ্যালয়"],
    "chakri":["চাকরি"], "kaj":["কাজ"], "bari":["বাড়ি","বাড়ি"],
    "ghor":["ঘর"], "manush":["মানুষ"], "lok":["লোক"],
    "meyeder":["মেয়েদের","মেয়েদের"], "cheleder":["ছেলেদের"],
    "kothay":["কোথায়","কোথায়"], "ki":["কি"], "keno":["কেন"],
    "kobe":["কবে"], "kemon":["কেমন"], "kivabe":["কিভাবে","কীভাবে"],
    "ki bhabe":["কি ভাবে","কীভাবে"], "khobor":["খবর"],
    "aj":["আজ"], "aaj":["আজ"], "kal":["কাল"],
    "bangla":["বাংলা"], "banglay":["বাংলায়","বাংলায়"],
    "kolkata":["কলকাতা"], "west bengal":["পশ্চিমবঙ্গ"],
}

_BENGLISH_MULTI = [
    ("ksh","ক্ষ"),("ng","ং"),("nj","ঞ্জ"),("nc","ঞ্চ"),("chh","ছ"),
    ("jh","ঝ"),("kh","খ"),("gh","ঘ"),("th","থ"),("dh","ধ"),
    ("ph","ফ"),("bh","ভ"),("sh","শ"),("ch","চ"),("tr","ত্র"),
    ("dr","দ্র"),("pr","প্র"),("br","ব্র"),("kr","ক্র"),("gr","গ্র"),
    ("st","স্ট"),("sk","স্ক"),("sp","স্প"),("sm","স্ম"),("sw","স্ব"),
]
_BENGLISH_C = {
    "k":"ক","g":"গ","c":"ক","j":"জ","t":"ত","d":"দ","n":"ন",
    "p":"প","b":"ব","m":"ম","y":"য","r":"র","l":"ল","s":"স",
    "h":"হ","v":"ভ","w":"ও","f":"ফ","q":"ক","x":"ক্স","z":"জ",
}
_BENGLISH_V = {"a":"া","i":"ি","u":"ু","e":"ে","o":"ো"}

def _benglish_phonetic_word(word: str) -> str:
    w = re.sub(r"[^a-z]", "", str(word or "").lower())
    if not w: return ""
    if w in _BENGLISH_PHRASE_ALIASES:
        return _BENGLISH_PHRASE_ALIASES[w][0]
    out=[]; i=0; pending=False
    while i < len(w):
        hit=None
        for latin,beng in _BENGLISH_MULTI:
            if w.startswith(latin,i):
                hit=(latin,beng); break
        if hit:
            out.append(hit[1]); i+=len(hit[0]); pending=True; continue
        if w.startswith("aa",i):
            out.append("া" if pending else "আ"); i+=2; pending=False; continue
        if w.startswith(("ee","ii"),i):
            out.append("ী" if pending else "ঈ"); i+=2; pending=False; continue
        if w.startswith(("oo","uu"),i):
            out.append("ূ" if pending else "ঊ"); i+=2; pending=False; continue
        ch=w[i]
        if ch in _BENGLISH_V:
            out.append(_BENGLISH_V[ch] if pending else {"a":"অ","i":"ই","u":"উ","e":"এ","o":"ও"}[ch])
            pending=False
        elif ch in _BENGLISH_C:
            out.append(_BENGLISH_C[ch]); pending=True
        else:
            out.append(ch); pending=False
        i+=1
    return "".join(out)

@lru_cache(maxsize=2048)
def _benglish_search_variants_cached(query: str) -> tuple:
    q=re.sub(r"\s+"," ",str(query or "").strip().lower())
    if not q or re.search(r"[\u0980-\u09ff]",q): return ()
    variants=[]
    if q in _BENGLISH_PHRASE_ALIASES:
        variants.extend(_BENGLISH_PHRASE_ALIASES[q])
    words=q.split()
    mapped=[_BENGLISH_PHRASE_ALIASES.get(w,[None])[0] for w in words]
    if mapped and all(mapped):
        variants.append(" ".join(mapped))
    mixed=[_BENGLISH_PHRASE_ALIASES.get(w,[w])[0] for w in words]
    if any(a!=b for a,b in zip(mixed,words)):
        variants.append(" ".join(mixed))
    if words:
        generic=" ".join(_benglish_phonetic_word(w) for w in words)
        if generic.strip(): variants.append(generic)
    compact=q.replace(" ","")
    if compact:
        variants.append(_benglish_phonetic_word(compact))
    return tuple(dict.fromkeys(v for v in variants if v and v!=q))


def _benglish_search_variants(query: str) -> list[str]:
    return list(_benglish_search_variants_cached(str(query or "")))


SEARCH_CONCEPT_ALIASES = {
    "master computer application": {
        "mca", "master of computer application", "master of computer applications",
    },
    "master business administration": {
        "mba", "master of business administration",
    },
    "bachelor technology": {
        "btech", "b tech", "bachelor of technology",
    },
    "bachelor engineering": {
        "be", "b e", "bachelor of engineering",
    },
    "bachelor science": {
        "bsc", "b sc", "bachelor of science",
    },
    "master science": {
        "msc", "m sc", "master of science",
    },
    "artificial intelligence": {
        "ai", "artificial intelligence",
    },
    "machine learning": {
        "ml", "machine learning",
    },
    "deep learning": {
        "dl", "deep learning",
    },
    "natural language processing": {
        "nlp", "natural language processing",
    },
    "higher education": {
        "higher ed", "university", "universities", "college", "colleges",
    },
    "national education policy": {
        "nep", "nep 2020", "national education policy 2020",
    },
    "university grants commission": {
        "ugc", "university grants commission",
    },
    "information technology": {
        "it", "information technology",
    },
    "computer science": {
        "cs", "cse", "computer science", "computer science engineering",
    },
    "research and development": {
        "r&d", "research development", "research and development",
    },
}

# Normalised once at import time instead of on every call.
_SEARCH_CONCEPT_TABLE = tuple(
    (
        _normalise_search(canonical),
        frozenset(_normalise_search(v) for v in values),
    )
    for canonical, values in SEARCH_CONCEPT_ALIASES.items()
)


def _search_concept_aliases(query: str) -> set[str]:
    """Return normalized aliases/concepts relevant to the user's query."""
    q = _normalise_search(query)
    if not q:
        return set()

    aliases = set()
    for canonical_n, value_norms in _SEARCH_CONCEPT_TABLE:
        if canonical_n in q or any(v and v in q for v in value_norms):
            aliases.add(canonical_n)
            aliases.update(v for v in value_norms if v)

    aliases.add(q)
    return aliases


def _prepare_concept(concept: str) -> tuple:
    """Pre-normalise a concept once per search request (not once per article)."""
    concept_n = _normalise_search(concept)
    compact_concept = _compact_search(concept)
    tokens = [t for t in concept_n.split() if t not in SEARCH_STOP_WORDS]
    return (concept_n, compact_concept, tuple(_token_variants(t) for t in tokens))


def _concept_matches(text_n: str, compact_text: str, words, prepared_concept: tuple) -> bool:
    """Match a prepared concept by exact phrase, compact acronym, or token coverage."""
    concept_n, compact_concept, token_variant_sets = prepared_concept
    if not text_n or not concept_n:
        return False
    if concept_n in text_n:
        return True
    if compact_concept and len(compact_concept) >= 3 and compact_concept in compact_text:
        return True
    if not token_variant_sets:
        return False
    return all(not words.isdisjoint(variants) for variants in token_variant_sets)


def _search_text_has_concept_prepared(
    text_n: str,
    compact_text: str,
    words: set[str],
    concept: str,
) -> bool:
    """Match a concept using already-prepared article text."""
    return _concept_matches(text_n, compact_text, words, _prepare_concept(concept))


def _search_text_has_concept(text: str, concept: str) -> bool:
    """Match a concept by exact phrase, compact acronym, or token coverage."""
    text_n = _normalise_search(text)
    return _concept_matches(
        text_n,
        _compact_search(text),
        set(text_n.split()),
        _prepare_concept(concept),
    )


def _prepare_search_context(query: str) -> dict:
    """Precompute query-only search signals once per search request."""
    q_phrase = _normalise_search(query)
    q_tokens = _query_tokens(query)
    aliases = _search_concept_aliases(query)
    benglish_variants = _benglish_search_variants(query)
    return {
        "q": q_phrase,
        "q_compact": _compact_search(query),
        "q_tokens": q_tokens,
        "q_phrase": q_phrase,
        "aliases": aliases,
        "benglish_variants": benglish_variants,
        "entity_codes": _search_entity_codes(query),
        # Prepared once so the per-article loop does no query normalisation.
        "q_token_variants": [_token_variants(t) for t in q_tokens],
        "alias_concepts": [_prepare_concept(a) for a in aliases if a != q_phrase],
        "benglish_concepts": [_prepare_concept(v) for v in benglish_variants],
        "benglish_pairs": [(v, _compact_search(v)) for v in benglish_variants],
    }


def _search_match_profile(
    query: str,
    article: Article,
    source: Source,
    search_context: dict | None = None,
    fields: Optional["_ArticleSearchFields"] = None,
) -> dict:
    """Produce explainable field-level relevance signals for one article."""
    context = search_context or _prepare_search_context(query)
    f = fields or _article_fields(article)

    title = f.title
    summary = f.summary
    title_compact = f.title_compact
    summary_compact = f.summary_compact
    title_words = f.title_words
    summary_words = f.summary_words
    category = _normalise_short(article.category)
    source_name = _normalise_short(source.name)
    category_words = frozenset(category.split())
    source_words = frozenset(source_name.split())

    q_tokens = context["q_tokens"]
    q_phrase = context["q_phrase"]
    token_variants = context["q_token_variants"]

    title_hits = sum(1 for v in token_variants if not title_words.isdisjoint(v))
    summary_hits = sum(1 for v in token_variants if not summary_words.isdisjoint(v))
    category_hits = sum(1 for v in token_variants if not category_words.isdisjoint(v))
    source_hits = sum(1 for v in token_variants if not source_words.isdisjoint(v))

    exact_title = bool(q_phrase and q_phrase in title)
    exact_summary = bool(q_phrase and q_phrase in summary)

    alias_concepts = context["alias_concepts"]
    alias_title = any(
        _concept_matches(title, title_compact, title_words, pc)
        for pc in alias_concepts
    )
    alias_summary = any(
        _concept_matches(summary, summary_compact, summary_words, pc)
        for pc in alias_concepts
    )

    benglish_pairs = context["benglish_pairs"]
    benglish_concepts = context["benglish_concepts"]
    benglish_title = any(
        v in title or vc in title_compact
        for v, vc in benglish_pairs
    )
    benglish_summary = any(
        v in summary or vc in summary_compact
        for v, vc in benglish_pairs
    )
    benglish_title_hits = sum(
        1 for pc in benglish_concepts
        if _concept_matches(title, title_compact, title_words, pc)
    )
    benglish_summary_hits = sum(
        1 for pc in benglish_concepts
        if _concept_matches(summary, summary_compact, summary_words, pc)
    )

    return {
        "title_hits": title_hits,
        "summary_hits": summary_hits,
        "category_hits": category_hits,
        "source_hits": source_hits,
        "token_count": len(q_tokens),
        "exact_title": exact_title,
        "exact_summary": exact_summary,
        "alias_title": alias_title,
        "alias_summary": alias_summary,
        "benglish_title": benglish_title,
        "benglish_summary": benglish_summary,
        "benglish_title_hits": benglish_title_hits,
        "benglish_summary_hits": benglish_summary_hits,
        "title": title,
        "summary": summary,
        "category": category,
        "source": source_name,
    }


def _score_search_result(
    query: str,
    article: Article,
    source: Source,
    search_context: dict | None = None,
) -> float:
    """
    Multi-signal relevance ranker.

    Strongest signals:
      exact phrase > complete title coverage > concept/abbreviation match
      > title proximity > summary coverage > metadata > recency.

    Fuzzy matching is deliberately weak and only activates after meaningful
    lexical evidence exists, preventing unrelated articles from floating up.

    Scoring rules are unchanged; only repeated normalisation work was removed.
    """
    context = search_context or _prepare_search_context(query)
    q = context["q"]
    q_compact = context["q_compact"]
    q_tokens = context["q_tokens"]

    f = _article_fields(article)
    title = f.title
    title_compact = f.title_compact
    summary_compact = f.summary_compact

    score = 0.0

    # HARD ENTITY IDENTITY GATE.
    requested_entity_codes = context.get("entity_codes")
    if requested_entity_codes is None:
        requested_entity_codes = _search_entity_codes(query)
    if requested_entity_codes:
        if not requested_entity_codes.intersection(f.entity_codes):
            return -1.0
        score += 4000

    profile = _search_match_profile(
        query,
        article,
        source,
        search_context=context,
        fields=f,
    )

    short_identifier = (
        len(q_tokens) >= 2
        and all(len(t) <= 4 for t in q_tokens)
        and len(q_compact) <= 12
    )

    exact_compact = bool(q_compact) and (
        q_compact in title_compact or q_compact in summary_compact
    )

    all_tokens_title = bool(q_tokens) and profile["title_hits"] == len(q_tokens)
    all_tokens_summary = bool(q_tokens) and profile["summary_hits"] == len(q_tokens)
    concept_match = profile["alias_title"] or profile["alias_summary"]
    benglish_match = profile["benglish_title"] or profile["benglish_summary"]

    # HARD RELEVANCE GATE.
    # A multi-word query must be fully represented, or have an explicit
    # concept/abbreviation match. This prevents one common word from winning.
    if short_identifier and not (
        exact_compact or all_tokens_title or all_tokens_summary or concept_match or benglish_match
    ):
        return -1.0

    if len(q_tokens) >= 2 and not (
        all_tokens_title or all_tokens_summary or exact_compact or concept_match or benglish_match
    ):
        return -1.0

    # EXACT / HIGH-CONFIDENCE MATCHES.
    if profile["exact_title"]:
        score += 3200
    if profile["exact_summary"]:
        score += 500
    if exact_compact and q_compact != q:
        score += 2400
    if all_tokens_title:
        score += 1800
    elif all_tokens_summary:
        score += 700

    # Explicit concept equivalence, e.g.:
    # "Master of Computer Application" <-> "MCA"
    # "Artificial Intelligence" <-> "AI"
    if concept_match:
        score += 1550
        if profile["alias_title"]:
            score += 750

    # Binglish -> Bengali phonetic/concept match.
    if benglish_match:
        score += 2100
        if profile["benglish_title"]:
            score += 1100
        score += profile["benglish_title_hits"] * 320
        score += profile["benglish_summary_hits"] * 90

    # FIELD COVERAGE.
    token_count = max(1, len(q_tokens))
    title_coverage = profile["title_hits"] / token_count
    summary_coverage = profile["summary_hits"] / token_count

    score += title_coverage * 1100
    score += summary_coverage * 300
    score += profile["category_hits"] * 45
    score += profile["source_hits"] * 30
    score += profile["title_hits"] * 180
    score += profile["summary_hits"] * 55

    # TITLE PROXIMITY.
    title_words = f.title_tokens
    if len(q_tokens) >= 2 and title_words:
        variant_sets = context["q_token_variants"]
        q_len = len(variant_sets)
        best_window = 999

        for i, word in enumerate(title_words):
            if word not in variant_sets[0]:
                continue

            matched = 1
            for j in range(1, q_len):
                if i + j >= len(title_words):
                    break
                if title_words[i + j] in variant_sets[j]:
                    matched += 1
                else:
                    break

            if matched == q_len:
                best_window = q_len
                break

            window_end = min(len(title_words), i + q_len + 4)
            window = title_words[i:window_end]
            hits = sum(
                1 for variants in variant_sets
                if not variants.isdisjoint(window)
            )
            if hits >= max(2, q_len - 1):
                best_window = min(best_window, len(window))

        if best_window < 999:
            score += max(
                120,
                520 - max(0, best_window - q_len) * 80
            )

    # TYPO TOLERANCE ONLY AFTER STRONG MATCH EVIDENCE.
    if (
        not short_identifier
        and q_tokens
        and profile["title_hits"] >= max(1, len(q_tokens) - 1)
    ):
        ratio = difflib.SequenceMatcher(None, q, title).ratio()
        score += ratio * 180

    # RECENCY IS ONLY A TIE-BREAKER.
    if article.published_at:
        try:
            now = datetime.now(timezone.utc)
            published = article.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - published).total_seconds() / 86400)
            score += max(0, 35 - min(age_days, 35))
        except Exception:
            pass

    return score


def _prefix_similarity(typed: str, candidate: str) -> float:
    """Prefix similarity that tolerates a small typo, e.g. crid -> cricket."""
    typed = _normalise_search(typed)
    candidate = _normalise_search(candidate)
    if not typed or not candidate:
        return 0.0
    if candidate.startswith(typed):
        return 1.0
    common = 0
    for a, b in zip(typed, candidate):
        if a != b:
            break
        common += 1
    return common / max(1, len(typed))


def _suggestion_match_score(query: str, candidate: str) -> float:
    """Score a suggestion like an autocomplete engine, not like article search."""
    q = _normalise_search(query)
    c = _normalise_search(candidate)
    if not q or not c:
        return -1.0

    q_tokens = q.split()
    c_tokens = c.split()
    score = 0.0

    if c == q:
        return 5000.0
    if c.startswith(q):
        score += 2200
    elif q in c:
        score += 900

    # Single-word prediction: "crid" -> "cricket", "cricbuzz", "cricinfo".
    first_token = c_tokens[0] if c_tokens else c
    sim = _prefix_similarity(q_tokens[-1], first_token)
    score += sim * 1300

    # Multi-word prediction: match the typed words in order and reward a
    # candidate that continues the phrase naturally.
    matched = 0
    for i, qt in enumerate(q_tokens):
        if i >= len(c_tokens):
            break
        if c_tokens[i] == qt:
            matched += 1
            score += 500
        else:
            token_sim = _prefix_similarity(qt, c_tokens[i])
            if token_sim >= 0.60:
                matched += 1
                score += token_sim * 420

    if q_tokens:
        score += (matched / len(q_tokens)) * 900

    # Short, natural autocomplete phrases are preferred to very long headlines.
    score += max(0, 180 - max(0, len(c_tokens) - 3) * 18)

    # Fuzzy fallback helps with small typing mistakes.
    score += difflib.SequenceMatcher(None, q, c[: max(len(q), min(len(c), len(q) + 18))]).ratio() * 180

    return score


def _add_suggestion(bucket: dict, text: str, score: float, kind: str = "query", meta: str = ""):
    cleaned = _clean_search_text(text)
    if not cleaned:
        return
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    if len(cleaned) < 2 or len(cleaned) > 140:
        return
    key = _normalise_search(cleaned)
    if not key:
        return
    current = bucket.get(key)
    if not current or score > current["score"]:
        bucket[key] = {
            "text": cleaned,
            "score": score,
            "kind": kind,
            "meta": meta,
        }


def _title_phrase_candidates(title: str, query: str):
    """Create full-title, word and short phrase predictions from indexed headlines."""
    clean_title = _clean_search_text(title)
    words = clean_title.split()
    if not words:
        return []

    q_tokens = _normalise_search(query).split()
    candidates = [clean_title]

    # Single-word predictions and short natural phrases.
    for i, word in enumerate(words):
        if q_tokens and _prefix_similarity(q_tokens[-1], _normalise_search(word)) >= 0.60:
            candidates.append(word)
            for n in range(2, 6):
                if i + n <= len(words):
                    candidates.append(" ".join(words[i:i+n]))

    # If the user typed multiple words, preserve the typed phrase and extend it
    # with words from the headline where possible.
    if q_tokens:
        normalized_words = [_normalise_search(w) for w in words]
        for i in range(max(0, len(words) - 8)):
            matched = 0
            for j, qt in enumerate(q_tokens):
                if i + j >= len(normalized_words):
                    break
                if normalized_words[i+j] == qt or _prefix_similarity(qt, normalized_words[i+j]) >= 0.60:
                    matched += 1
                else:
                    break
            if matched == len(q_tokens):
                end = min(len(words), i + len(q_tokens) + 3)
                candidates.append(" ".join(words[i:end]))

    return candidates



@app.get("/api/popular-searches")
def popular_searches(
    limit: int = Query(default=8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """
    Return the most frequently searched queries recorded by AMI.
    This endpoint is read-only and does not consume search quota.
    """
    rows = db.execute(
        select(SearchQueryLog)
        .where(SearchQueryLog.normalized_query != "")
        .order_by(
            SearchQueryLog.search_count.desc(),
            SearchQueryLog.last_searched_at.desc(),
        )
        .limit(100)
    ).scalars().all()

    seen = set()
    items = []

    for row in rows:
        label = (row.query or "").strip()
        normalized = (row.normalized_query or "").strip()

        if not label or not normalized or normalized in seen:
            continue

        # Avoid exposing extremely short/non-meaningful terms as popular chips.
        if len(normalized) < 2:
            continue

        seen.add(normalized)
        items.append({
            "query": label,
            "search_count": int(row.search_count or 0),
            "last_searched_at": (
                row.last_searched_at.isoformat()
                if row.last_searched_at else None
            ),
        })

        if len(items) >= limit:
            break

    return {
        "status": "success",
        "items": items,
    }


@app.get("/api/search/suggestions")
def search_suggestions(
    q: str = Query(min_length=1, max_length=100),
    db: Session = Depends(get_db),
):
    """
    Fast Google-style predictive suggestions.

    Uses:
      1. Previously searched queries.
      2. Recent matching article headlines.
      3. Source names.
      4. Categories.

    This endpoint does not consume search quota.
    """
    query_text = _normalise_search(q)
    if not query_text:
        return {"status": "success", "query": q, "suggestions": []}

    bucket = {}
    now = datetime.now(timezone.utc)

    # ---------------------------------------------------------
    # 1. Search history
    # ---------------------------------------------------------
    history_rows = db.execute(
        select(SearchQueryLog)
        .where(
            SearchQueryLog.normalized_query.ilike(f"{query_text}%")
        )
        .order_by(
            SearchQueryLog.search_count.desc(),
            SearchQueryLog.last_searched_at.desc(),
        )
        .limit(20)
    ).scalars().all()

    for row in history_rows:
        age_days = 0
        if row.last_searched_at:
            ts = row.last_searched_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - ts).total_seconds() / 86400)

        recency = max(0, 100 - min(age_days, 100))
        score = _suggestion_match_score(q, row.query)
        score += min(700, row.search_count * 45)
        score += recency

        _add_suggestion(bucket, row.query, score, "history")

    # ---------------------------------------------------------
    # 2. Matching article titles only
    #
    # IMPORTANT:
    # Do not load 1,500 newest articles and then filter them
    # in Python. Let PostgreSQL filter first.
    # ---------------------------------------------------------
    pattern = f"%{query_text}%"

    article_rows = db.execute(
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
        .where(
            or_(
                Article.title.ilike(pattern),
                Article.summary.ilike(pattern),
            )
        )
        .order_by(Article.published_at.desc())
        .limit(100)
    ).all()

    for article, source in article_rows:
        title = _clean_search_text(article.title)
        if not title:
            continue

        title_score = _suggestion_match_score(q, title)
        if title_score < 450:
            continue

        for candidate in _title_phrase_candidates(title, q):
            score = _suggestion_match_score(q, candidate)

            if candidate == title:
                score += 120

                if article.published_at:
                    published = article.published_at
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)

                    age_days = max(
                        0,
                        (now - published).total_seconds() / 86400,
                    )
                    score += max(0, 120 - min(age_days, 120))
            else:
                score += 80

            _add_suggestion(
                bucket,
                candidate,
                score,
                "article",
                source.name if source else "",
            )

    # ---------------------------------------------------------
    # 3. Source names
    # ---------------------------------------------------------
    source_rows = db.execute(
        select(Source.name)
        .where(
            Source.name.is_not(None),
            Source.name.ilike(pattern),
        )
        .limit(30)
    ).all()

    for (name,) in source_rows:
        score = _suggestion_match_score(q, name)
        if score >= 450:
            _add_suggestion(bucket, name, score + 120, "source")

    # ---------------------------------------------------------
    # 4. Categories
    # ---------------------------------------------------------
    category_rows = db.execute(
        select(Article.category)
        .where(
            Article.category.is_not(None),
            Article.category.ilike(pattern),
        )
        .distinct()
        .limit(30)
    ).all()

    for (category,) in category_rows:
        if not category:
            continue

        score = _suggestion_match_score(q, category)
        if score >= 450:
            _add_suggestion(bucket, category, score + 60, "category")

    # ---------------------------------------------------------
    # 5. Final compact result
    # ---------------------------------------------------------
    ranked = sorted(
        bucket.values(),
        key=lambda item: (
            -item["score"],
            item["text"].casefold(),
        ),
    )

    suggestions = [
        item["text"]
        for item in ranked[:10]
    ]

    return {
        "status": "success",
        "query": q,
        "suggestions": suggestions,
    }
@app.get("/api/search/diagnostic")
def search_diagnostic(
    q: str = Query(min_length=1, max_length=200),
    db: Session = Depends(get_db),
):
    """
    Explain why a query does or does not have indexed matches.

    This endpoint does not consume search quota. It is intentionally
    diagnostic rather than a second search API, so the frontend can explain
    an empty result without weakening the normal relevance gates.
    """
    query_text = _normalise_search(q)
    tokens = _query_tokens(q)
    iit_codes = sorted(_iit_campus_codes(q))
    iim_codes = sorted(_institution_campus_codes(q))
    entity_codes = sorted(_search_entity_codes(q))

    token_matches = {}
    for token in tokens[:12]:
        variants = _token_variants(token)
        conditions = []
        for variant in variants:
            pattern = f"%{variant}%"
            conditions.extend([
                Article.title.ilike(pattern),
                Article.summary.ilike(pattern),
                    Source.name.ilike(pattern),
            ])
        token_matches[token] = db.scalar(
            select(func.count())
            .select_from(Article)
            .join(Source, Article.source_id == Source.id)
            .where(or_(*conditions))
        ) or 0

    entity_matches = 0
    if entity_codes:
        entity_rows = db.execute(
            select(Article, Source)
            .join(Source, Article.source_id == Source.id)
            .where(
                or_(
                    Article.title.ilike("%iit%"),
                    Article.summary.ilike("%iit%"),
                    Article.category.ilike("%iit%"),
                    Article.title.ilike("%iim%"),
                    Article.summary.ilike("%iim%"),
                    Article.category.ilike("%iim%"),
                )
            )
            .limit(5000)
        ).all()
        for article, _source_row in entity_rows:
            article_codes = _article_fields(article).entity_codes
            if set(entity_codes).intersection(article_codes):
                entity_matches += 1

    return {
        "status": "success",
        "query": q,
        "normalized_query": query_text,
        "tokens": tokens,
        "entity": {
            "type": (
                "IIT" if iit_codes
                else "IIM" if iim_codes
                else None
            ),
            "iit_campus_codes": iit_codes,
            "iim_campus_codes": iim_codes,
            "canonical_entity_codes": entity_codes,
        },
        "indexed_coverage": {
            "token_matches": token_matches,
            "entity_matches": entity_matches,
        },
        "interpretation": (
            "Specific institution/campus query. Matching campus identity is "
            "required before a result can be returned."
            if entity_codes else
            "General keyword query. Results are retrieved from the local "
            "indexed article corpus and ranked by relevance."
        ),
        "quota_consumed": False,
    }


@app.get("/api/search/filter-options")
def search_filter_options(
    db: Session = Depends(get_db),
):
    """Public metadata used by the search filters on the main page."""
    source_rows = db.execute(
        select(Source.name)
        .where(Source.active == True)
        .where(Source.name.is_not(None))
        .order_by(Source.name.asc())
    ).all()

    category_rows = db.execute(
        select(Article.category)
        .where(Article.category.is_not(None))
        .distinct()
        .order_by(Article.category.asc())
    ).all()

    language_rows = db.execute(
        select(Article.language)
        .where(Article.language.is_not(None))
        .distinct()
        .order_by(Article.language.asc())
    ).all()

    return {
        "status": "success",
        "sources": [name for (name,) in source_rows if name],
        "categories": [category for (category,) in category_rows if category],
        "languages": [language for (language,) in language_rows if language],
    }


@app.get("/api/search")
def search(
    q: str = Query(min_length=1, max_length=200),
    user_key: str = Header(alias="X-User-Key"),
    x_auth_token: Optional[str] = Header(None, alias="X-Auth-Token"),
    filter_only: bool = Header(False, alias="X-Filter-Only"),
    date_range: str = Query("30", max_length=10),
    categories: str = Query("", max_length=500),
    language: str = Query("", max_length=100),
    source: str = Query("", max_length=255),
    db: Session = Depends(get_db),
):
    subscriber = _subscriber_from_token(
        x_auth_token,
        db,
    )

    effective_user_key = (
        subscriber.user_key
        if subscriber
        else user_key
    )

    usage = db.scalar(
        select(Usage).where(
            Usage.user_key == effective_user_key
        )
    )

    if not usage:
        usage = Usage(
            user_key=effective_user_key,
            searches_used=0,
            subscribed=bool(subscriber),
        )
        db.add(usage)
        db.commit()
        db.refresh(usage)

    is_subscriber = bool(subscriber)

    # Read once per request (previously up to 4 identical queries).
    free_limit = get_free_search_limit(db)

    # A normal search that would exceed the free allowance is blocked.
    # Filter-only refreshes are allowed without consuming quota because the
    # user is refining an already executed search.
    if (
        not filter_only
        and not is_subscriber
        and usage.searches_used >= free_limit
    ):
        raise HTTPException(
            status_code=402,
            detail={
                "message": "Free search limit reached.",
                "free_limit": free_limit,
                "searches_used": usage.searches_used,
                "remaining": 0,
            },
        )

    # Prepare query-only ranking signals once and reuse them everywhere.
    search_context = _prepare_search_context(q)
    query_text = search_context["q"]
    tokens = search_context["q_tokens"]

    if not query_text:
        return {
            "status": "success",
            "query": q,
            "count": 0,
            "free_limit": free_limit,
            "searches_used": usage.searches_used,
            "remaining": (
                None if is_subscriber
                else max(0, free_limit - usage.searches_used)
            ),
            "subscribed": is_subscriber,
            "results": [],
        }

    # Candidate retrieval: require meaningful query terms somewhere in
    # title/summary/category/source. Ranking and entity-level matching happen
    # in Python after retrieval.
    token_conditions = []
    retrieval_terms = set(tokens[:12])

    # Concept aliases are evaluated by the Python ranker below.
    # Keeping them out of the normal SQL OR tree avoids expensive ILIKE predicates.
    # Entity-specific searches below retain their dedicated broad retrieval.

    # Bengali/Binglish variants are handled by the Python ranker.
    # Keeping them out of the normal SQL OR tree avoids expensive ILIKE predicates.
    # Dedicated entity retrieval below remains unchanged.

    # Fast SQL candidate retrieval.
    # PostgreSQL trigram indexes cover the article text columns.
    # Morphology, aliases and source relevance are handled by the Python ranker.
    # Keeping SQL to exact query tokens avoids a large OR/ILIKE explosion.
    for token in retrieval_terms:
        if len(token) < 2:
            continue
        pattern = f"%{token}%"
        token_conditions.extend([
            Article.title.ilike(pattern),
            Article.summary.ilike(pattern),
            Article.category.ilike(pattern),
        ])
    # Binglish: also retrieve Bengali-script articles through the phonetic
    # variants (e.g. "kolkata" -> "কলকাতা"). This was removed in 547ef1e
    # to save SQL time; with the pg_trgm indexes it is cheap again. The ranker
    # still decides whether each candidate is genuinely relevant.
    for bengali_variant in search_context["benglish_variants"]:
        if len(bengali_variant) >= 2:
            pattern = f"%{bengali_variant}%"
            token_conditions.extend([
                Article.title.ilike(pattern),
                Article.summary.ilike(pattern),
            ])

    # IIT campus identifiers need special candidate retrieval. For example,
    # "IIT-M" normalises to tokens ["iit", "m"], but a real article is
    # normally titled "IIT Madras", so requiring the literal token "m" in SQL
    # incorrectly eliminates the correct article before _iit_campus_codes()
    # gets a chance to resolve the alias. Retrieve IIT articles broadly and
    # apply the canonical campus gate below.
    requested_entity_codes = search_context["entity_codes"]
    if requested_entity_codes:
        # Entity aliases such as IIT-M / IIM-A need broad candidate retrieval;
        # the exact campus identity is enforced after SQL retrieval.
        entity_terms = []
        for prefix in ("iit", "iim"):
            entity_terms.extend([
                Article.title.ilike(f"%{prefix}%"),
                Article.summary.ilike(f"%{prefix}%"),
                Article.category.ilike(f"%{prefix}%"),
            ])
        search_conditions = [or_(*entity_terms)]
    else:
        # Identifier queries such as UGC, NEP-2020, etc. remain strict.
        # IIT queries are excluded from this branch because their campus
        # aliases require semantic resolution rather than literal token
        # matching.
        strict_identifier = (
            len(tokens) >= 2
            and all(len(t) <= 4 for t in tokens)
            and len(search_context["q_compact"]) <= 12
        )
        if strict_identifier:
            identifier_conditions = []
            for token in tokens:
                token_variants = _token_variants(token)
                identifier_conditions.append(
                    or_(*[
                        Article.title.ilike(f"%{v}%")
                        for v in token_variants
                    ])
                )
            search_conditions = [and_(*identifier_conditions)]
        else:
            search_conditions = [or_(*token_conditions)]

    # Apply the currently selected filters on the actual search request.
    # This is important because the quota decision must be based on results
    # the user can actually see, not on an unfiltered batch of 50 articles.

    if date_range != "all":
        try:
            days = int(date_range)
            if days > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                search_conditions.append(Article.published_at >= cutoff)
        except (TypeError, ValueError):
            pass

    if language.strip():
        search_conditions.append(Article.language.ilike(language.strip()))

    if source.strip():
        search_conditions.append(Source.name.ilike(source.strip()))

    # Category aliases mirror the filter labels in the frontend.
    selected_categories = [
        item.strip().lower()
        for item in categories.split(",")
        if item.strip() and item.strip().lower() != "all"
    ]
    if selected_categories:
        category_conditions = []
        for category in selected_categories:
            if category == "education":
                # Education is a semantic filter, not only a literal value in
                # Article.category. RSS feeds often store a generic category
                # such as "News", while the headline/summary clearly identifies
                # an education story. Include higher-education institutions such
                # as IIT/IIM/NIT so an IIT-B or IIT-M search remains visible.
                category_conditions.extend(_education_match_conditions())
            else:
                aliases = {
                    "government": ["%government%", "%policy%"],
                    "research": ["%research%", "%innovation%"],
                    "campus": ["%campus%"],
                    "technology": ["%technology%", "%tech%"],
                    "business": ["%business%"],
                    "national": ["%national%"],
                }.get(category, [f"%{category}%"])
                for pattern in aliases:
                    category_conditions.extend([
                                    Article.title.ilike(pattern),
                        Article.summary.ilike(pattern),
                    ])
        if category_conditions:
            search_conditions.append(or_(*category_conditions))

    candidate_query = (
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
    )
    # Newest candidates first. Without ORDER BY, PostgreSQL returned an
    # arbitrary 800 matches (usually the oldest rows) for common words.
    # Served by ix_articles_published_at_desc + the pg_trgm indexes.
    candidate_order = (
        Article.published_at.desc().nullslast(),
        Article.id.desc(),
    )

    multi_word_or = (
        not requested_entity_codes
        and not strict_identifier
        and len([t for t in retrieval_terms if len(t) >= 2]) >= 2
    )

    if multi_word_or:
        # Stage 1: rows containing EVERY query word. The ranker's hard gate
        # requires all words of a multi-word query, so these are the real
        # candidates. Previously a common word (e.g. "university") filled the
        # 800-row cap and pushed out rows containing the rarer word
        # (e.g. "jadavpur"). PostgreSQL answers this with a BitmapAnd of the
        # trigram indexes, so it is fast and selective.
        all_words_condition = and_(*[
            or_(
                Article.title.ilike(f"%{token}%"),
                Article.summary.ilike(f"%{token}%"),
                Article.category.ilike(f"%{token}%"),
            )
            for token in retrieval_terms
            if len(token) >= 2
        ])
        rows = db.execute(
            candidate_query
            .where(all_words_condition, *search_conditions[1:])
            .order_by(*candidate_order)
            .limit(SEARCH_CANDIDATE_LIMIT)
        ).all()

        # Stage 2: top up with the original any-word retrieval, so concept /
        # abbreviation / Binglish matches that the ranker accepts are kept.
        if len(rows) < SEARCH_CANDIDATE_LIMIT:
            seen_ids = [article.id for article, _source_row in rows]
            rows += db.execute(
                candidate_query
                .where(
                    *search_conditions,
                    *([Article.id.not_in(seen_ids)] if seen_ids else []),
                )
                .order_by(*candidate_order)
                .limit(SEARCH_CANDIDATE_LIMIT - len(rows))
            ).all()
    else:
        rows = db.execute(
            candidate_query
            .where(*search_conditions)
            .order_by(*candidate_order)
            .limit(SEARCH_CANDIDATE_LIMIT)
        ).all()

    # Campus-aware hard gate. SQL token matching intentionally remains broad
    # for performance, but an IIT campus identifier must resolve to the same
    # canonical campus before the article can be returned. This is what keeps
    # "IIT-B" from returning "IIT-M" simply because both contain "IIT".
    # Reuse the query-level entity codes already calculated above.
    if requested_entity_codes:
        rows = [
            (article, source_row)
            for article, source_row in rows
            if requested_entity_codes.intersection(_article_fields(article).entity_codes)
        ]

    ranked_rows = [
        (
            _score_search_result(
                q,
                article,
                source_row,
                search_context=search_context,
            ),
            article,
            source_row,
        )
        for article, source_row in rows
    ]

    ranked_rows.sort(
        key=lambda item: (
            -item[0],
            -(item[1].published_at.timestamp()
              if item[1].published_at
              else 0),
        )
    )

    # Remove duplicate/syndicated copies before pagination.
    deduped_rows = []
    seen_titles = set()

    for item in ranked_rows:
        score, article, source_row = item
        title_key = _article_fields(article).title
        title_key = re.sub(
            r"\b(update|breaking|live|latest)\b",
            " ",
            title_key,
        )
        title_key = re.sub(r"\s+", " ", title_key).strip()

        if title_key and title_key in seen_titles:
            continue
        if title_key:
            seen_titles.add(title_key)

        deduped_rows.append(item)

    ranked_rows = deduped_rows[:50]

    # Instant lexical / Binglish search only. No AI or external inference is
    # involved in the search critical path.
    top_score = ranked_rows[0][0] if ranked_rows else None

    # IMPORTANT: a zero-result search does NOT consume quota.
    result_count = len(ranked_rows)

    # Only a new user search is counted. Filter-only refreshes are generated
    # by changing Date/Category/Language/Source and must not consume quota or
    # alter search popularity statistics.
    if not filter_only:
        existing_query = db.scalar(
            select(SearchQueryLog).where(
                SearchQueryLog.normalized_query == query_text
            )
        )
        if existing_query:
            existing_query.search_count += 1
            existing_query.last_searched_at = datetime.now(timezone.utc)
            existing_query.query = q.strip()
        else:
            db.add(
                SearchQueryLog(
                    query=q.strip(),
                    normalized_query=query_text,
                    search_count=1,
                    last_searched_at=datetime.now(timezone.utc),
                )
            )

        if result_count > 0 and not is_subscriber:
            usage.searches_used += 1

    # Serialise the response BEFORE commit. db.commit() expires every ORM
    # object, so reading article/source attributes afterwards re-SELECTed each
    # of the 50 articles and their sources one by one (~88 extra queries per
    # search, each paying the full network round trip to Render PostgreSQL).
    searches_used = usage.searches_used
    results_payload = [
        {
            "id": article.id,
            "relevance_score": round(score, 2),
            "is_most_relevant": bool(
                top_score is not None and score == top_score
            ),
            "relevance_label": (
                "Most relevant"
                if top_score is not None and score == top_score
                else "Relevant"
            ),
            "title": article.title,
            "url": article.url,
            "image_url": article.image_url,
            "summary": article.summary,
            "category": article.category,
            "language": article.language,
            "published_at": article.published_at,
            "source": source_row.name,
            "source_website": source_row.website,
        }
        for score, article, source_row in ranked_rows
    ]

    db.commit()

    remaining = max(
        0,
        free_limit - searches_used,
    )

    return {
        "status": "success",
        "query": q,
        "count": result_count,
        "free_limit": free_limit,
        "searches_used": searches_used,
        "remaining": (
            None if is_subscriber else remaining
        ),
        "subscribed": is_subscriber,
        "search_language_mode": (
            "Binglish → Bengali"
            if search_context["benglish_variants"]
            else ("Bengali" if re.search(r"[ঀ-৿]", q) else "English / Mixed")
        ),
        "search_variants": search_context["benglish_variants"][:8],
        "filters": {
            "date_range": date_range,
            "categories": selected_categories,
            "language": language.strip(),
            "source": source.strip(),
        },
        "results": results_payload,
    }


_STOPWORDS = {
    "the","and","for","with","from","this","that","into","after","before","about","over","under","news","india","latest","says","said","will","has","have","are","was","were","its","their","they","more","than","new","today","how","why","what","when","where","who","amid","among","during","through","against","also","been","being","not","but","his","her","our","your","you","all","one","two","three"
}
_POSITIVE = {"growth","success","launch","win","wins","benefit","improve","improved","progress","rise","rises","record","award","achievement","expansion","boost","gain"}
_NEGATIVE = {"crisis","loss","fall","falls","decline","attack","fraud","scandal","death","dead","conflict","warning","risk","probe","arrest","seized","collapse","strike"}

def _intel_clean(text: str) -> str:
    """Return readable article text without escaped HTML/URL artefacts."""
    value = str(text or "")
    # Decode entities first. Otherwise &lt;strong&gt; becomes "strong" after tag stripping.
    for _ in range(2):
        decoded = html_lib.unescape(value)
        if decoded == value:
            break
        value = decoded
    value = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    # URLs, e-mail addresses and escaped URL fragments should never become topics.
    value = re.sub(r"https?://\S+|www\.\S+", " ", value, flags=re.I)
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", " ", value)
    value = re.sub(r"(?:https?[:/\\]|www\s*\.)", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


_INTEL_TOPIC_ARTIFACTS = {
    "nbsp", "amp", "quot", "apos", "lt", "gt", "strong", "span",
    "style", "script", "div", "class", "href", "http", "https", "www",
    "com", "org", "net", "html", "first", "read", "click", "here",
    "copyright", "follow", "subscribe", "share", "login", "sign", "email",
    "news18", "indiatoday", "timesofindia", "latest", "breaking", "watch",
}

def _intel_topic_tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", _intel_clean(text).lower())
    return [w for w in words if w not in _STOPWORDS and w not in _INTEL_TOPIC_ARTIFACTS]

def _intel_tokens(text: str) -> list[str]:
    return _intel_topic_tokens(text)

def _intel_topic_ngrams(text: str, max_n: int = 3) -> list[str]:
    tokens = _intel_topic_tokens(text)
    out = []
    for n in range(2, max_n + 1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i:i+n])
            # Avoid phrases made only of generic/noisy words.
            if len(phrase) >= 7 and phrase not in out:
                out.append(phrase)
    return out

def _intel_topic_candidates(rows, previous_rows, limit: int = 80) -> list[dict]:
    """Build document-level topic velocity efficiently.

    Each article contributes at most one mention to a topic. Candidate topics are
    generated once per document and then counted with Counters, avoiding the
    previous candidate x document x token scan that became slow on 30/90-day data.
    """
    current_counts = Counter()
    previous_counts = Counter()
    candidate_counts = Counter()

    def collect(items, bucket):
        for a, _sr in items:
            title = _intel_clean(a.title or "")
            summary = _intel_clean(a.summary or "")
            # Headlines carry the strongest topic signal; a short summary fallback
            # adds context without making the analytics endpoint process huge HTML blobs.
            text = title if title else summary[:1200]
            if not text:
                continue
            token_list = _intel_topic_tokens(text)
            if not token_list:
                continue
            token_set = set(token_list)
            phrases = set(_intel_topic_ngrams(text, 3))

            # Named multi-word entities are strong topic labels.
            for entity in _intel_entities(text, limit=12):
                ew = _intel_topic_tokens(entity)
                if len(ew) >= 2:
                    phrases.add(" ".join(ew))

            # Meaningful single terms require repeated document evidence later.
            phrases.update(w for w in token_set if len(w) >= 5)
            phrases = {
                p for p in phrases
                if len(p) >= 4 and p not in _INTEL_TOPIC_ARTIFACTS
            }
            bucket.update(phrases)
            candidate_counts.update(phrases)

    # Candidate generation is linear in the number of indexed articles.
    collect(rows, current_counts)
    # Candidate pool must include previous-period topics so cooling topics remain visible.
    for a, _sr in previous_rows:
        title = _intel_clean(a.title or "")
        summary = _intel_clean(a.summary or "")
        text = title if title else summary[:1200]
        if not text:
            continue
        phrases = set(_intel_topic_ngrams(text, 3))
        for entity in _intel_entities(text, limit=12):
            ew = _intel_topic_tokens(entity)
            if len(ew) >= 2:
                phrases.add(" ".join(ew))
        phrases.update(w for w in set(_intel_topic_tokens(text)) if len(w) >= 5)
        phrases = {p for p in phrases if len(p) >= 4 and p not in _INTEL_TOPIC_ARTIFACTS}
        previous_counts.update(phrases)
        candidate_counts.update(phrases)

    scored = []
    for topic, evidence in candidate_counts.items():
        current = current_counts.get(topic, 0)
        previous = previous_counts.get(topic, 0)
        # Require enough document evidence to avoid one-off words/phrases.
        if current < 2 and previous < 2:
            continue
        if " " not in topic and current < 3 and previous < 3:
            continue
        scored.append((topic, current, previous, evidence))

    scored.sort(
        key=lambda x: (" " in x[0], x[1], x[1] + x[2], x[3], len(x[0])),
        reverse=True,
    )

    deduped = []
    for topic, current, previous, _evidence in scored:
        if any(topic in existing or existing in topic for existing, *_ in deduped[:25]):
            continue
        if previous == 0:
            velocity = None
            status = "New"
        else:
            velocity = round(((current - previous) / previous) * 100, 1)
            if velocity >= 50:
                status = "Accelerating"
            elif velocity >= 10:
                status = "Rising"
            elif velocity <= -20:
                status = "Cooling"
            else:
                status = "Stable"
        current_total = max(1, len(rows))
        previous_total = max(1, len(previous_rows))
        deduped.append({
            "topic": topic.title() if topic.islower() else topic,
            "mentions": current,
            "previous_mentions": previous,
            "mention_rate_per_100_articles": round((current / current_total) * 100, 2),
            "previous_rate_per_100_articles": round((previous / previous_total) * 100, 2),
            "velocity_pct": velocity,
            "status": status,
        })
        if len(deduped) >= limit:
            break
    return deduped

def _intel_keywords(text: str, limit: int = 8) -> list[str]:
    return [w for w,_ in Counter(_intel_tokens(text)).most_common(limit)]

def _intel_summary(article: Article) -> str:
    raw = _intel_clean(article.summary or "")
    if raw:
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", raw) if x.strip()]
        if sentences:
            return " ".join(sentences[:2])[:500]
    return _intel_clean(article.title or "")[:500]

def _intel_sentiment(text: str) -> str:
    toks = set(_intel_tokens(text))
    p = len(toks & _POSITIVE); n = len(toks & _NEGATIVE)
    if p > n + 1: return "Positive"
    if n > p + 1: return "Negative"
    return "Neutral"

def _intel_entities(text: str, limit: int = 10) -> list[str]:
    clean = _intel_clean(text)
    patterns = re.findall(r"\b(?:[A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,3})\b", clean)
    out=[]
    for item in patterns:
        item=item.strip(" ,.:;()[]")
        if len(item) < 3 or item.lower() in _STOPWORDS: continue
        if item not in out: out.append(item)
    return out[:limit]

def _cluster_similarity(a: str, b: str) -> float:
    sa=set(_intel_tokens(a)); sb=set(_intel_tokens(b))
    if not sa or not sb: return 0.0
    j=len(sa&sb)/len(sa|sb)
    seq=difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return max(j, seq*0.72)

def rebuild_story_clusters(db: Session, limit: int = 700) -> dict:
    articles=db.scalars(select(Article).order_by(Article.published_at.desc().nullslast(), Article.id.desc()).limit(limit)).all()
    db.query(StoryClusterArticle).delete(synchronize_session=False)
    db.query(StoryCluster).delete(synchronize_session=False)
    db.commit()
    clusters=[]
    bucket=defaultdict(list)
    for article in articles:
        title=_intel_clean(article.title)
        toks=_intel_tokens(title)
        key_tokens=sorted(toks[:3]) if toks else [str(article.id)]
        candidates=[]
        for k in key_tokens:
            candidates.extend(bucket.get(k, []))
        best=None; best_score=0.0
        for cidx in dict.fromkeys(candidates):
            score=_cluster_similarity(title, clusters[cidx]["title"])
            if score>best_score: best_score=score; best=cidx
        if best is None or best_score < 0.38:
            cidx=len(clusters)
            clusters.append({"title":title,"summary":_intel_summary(article),"keywords":_intel_keywords(title),"category":article.category,"language":article.language,"first":article.published_at or article.created_at,"last":article.published_at or article.created_at,"articles":[]})
        else:
            cidx=best
            c=clusters[cidx]
            c["articles"].append(article)
            c["last"]=max(c["last"], article.published_at or article.created_at)
            c["keywords"]=_intel_keywords(c["title"]+" "+title)
        if article not in clusters[cidx]["articles"]:
            clusters[cidx]["articles"].append(article)
        for k in key_tokens: bucket[k].append(cidx)
    created=0
    for i,c in enumerate(clusters):
        if not c["articles"]: continue
        if len(c["articles"]) == 1 and len(clusters)>20:
            continue
        cluster=StoryCluster(cluster_key=f"CL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{i}-{secrets.token_hex(3)}", title=c["title"][:1000], summary=c["summary"], keywords=", ".join(c["keywords"]), category=c["category"], language=c["language"], first_seen_at=c["first"], last_updated_at=c["last"], article_count=len(c["articles"]))
        db.add(cluster); db.flush()
        for art in c["articles"]:
            db.add(StoryClusterArticle(cluster_id=cluster.id, article_id=art.id, similarity_score=_cluster_similarity(c["title"], _intel_clean(art.title))))
        created+=1
    db.commit()
    return {"clusters":created,"articles_processed":len(articles)}


def _require_paid_subscriber(user_key: str, token: Optional[str], db: Session) -> SubscriberAccount:
    sub=_subscriber_from_token(token,db)
    if not sub:
        raise HTTPException(status_code=403, detail="Save Search is available only to paid subscribers with an active subscription.")
    return sub

def _require_subscriber_or_free_user(user_key: str, token: Optional[str], db: Session) -> str:
    sub=_subscriber_from_token(token,db)
    return sub.user_key if sub else user_key

def _intel_article_payload(article: Article, source: Optional[Source]=None):
    text=f"{article.title} {article.summary or ''}"
    return {"id":article.id,"title":article.title,"url":article.url,"image_url":article.image_url,"summary":_intel_summary(article),"source":source.name if source else None,"category":article.category,"language":article.language,"published_at":article.published_at,"keywords":_intel_keywords(text),"entities":_intel_entities(text),"sentiment":_intel_sentiment(text)}

@app.get("/api/intelligence/dashboard")
def intelligence_dashboard(x_user_key: str = Header(alias="X-User-Key"), x_auth_token: Optional[str] = Header(None, alias="X-Auth-Token"), db: Session = Depends(get_db)):
    user_key=_require_subscriber_or_free_user(x_user_key,x_auth_token,db)
    now=datetime.now(timezone.utc); cutoff=now-timedelta(days=7)
    recent=db.execute(select(Article,Source).join(Source,Article.source_id==Source.id).where(or_(Article.published_at>=cutoff,Article.created_at>=cutoff)).order_by(Article.published_at.desc().nullslast()).limit(1000)).all()
    word_counts=Counter()
    lang=Counter(); cats=Counter(); sources=Counter()
    for a,sr in recent:
        word_counts.update(_intel_tokens(a.title+" "+(a.summary or "")))
        lang[a.language or "Unknown"]+=1; cats[a.category or "News"]+=1; sources[sr.name]+=1
    clusters=db.scalars(select(StoryCluster).order_by(StoryCluster.article_count.desc(),StoryCluster.last_updated_at.desc()).limit(12)).all()
    return {"status":"success","user_key":user_key,"is_subscriber":bool(_subscriber_from_token(x_auth_token,db)),"metrics":{"articles_7d":len(recent),"sources_7d":len(sources),"story_clusters":db.scalar(select(func.count()).select_from(StoryCluster)) or 0,"tracked_topics":db.scalar(select(func.count()).select_from(TrackedTopic).where(TrackedTopic.user_key==user_key,TrackedTopic.active==True)) or 0},"trending":[{"topic":k,"mentions":v} for k,v in word_counts.most_common(15)],"languages":[{"name":k,"count":v} for k,v in lang.most_common(10)],"categories":[{"name":k,"count":v} for k,v in cats.most_common(10)],"sources":[{"name":k,"count":v} for k,v in sources.most_common(10)],"clusters":[{"id":c.id,"title":c.title,"summary":c.summary,"article_count":c.article_count,"keywords":c.keywords,"last_updated_at":c.last_updated_at} for c in clusters]}


@app.get("/api/intelligence/analytics")
def intelligence_analytics(
    days: int = Query(7, ge=7, le=90),
    x_user_key: str = Header(alias="X-User-Key"),
    x_auth_token: Optional[str] = Header(None, alias="X-Auth-Token"),
    db: Session = Depends(get_db),
):
    """Executive analytics payload for the V6 dashboard.

    The endpoint deliberately returns measured database values only. Geographic
    coverage is inferred from the configured Source.coverage field, which is the
    platform's existing source-level geography metadata.
    """
    user_key = _require_subscriber_or_free_user(x_user_key, x_auth_token, db)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    previous_cutoff = cutoff - timedelta(days=days)

    rows = db.execute(
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
        .where(or_(Article.published_at >= cutoff, Article.created_at >= cutoff))
        .order_by(Article.published_at.asc().nullslast(), Article.created_at.asc())
    ).all()
    previous_rows = db.execute(
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
        .where(
            or_(
                and_(Article.published_at >= previous_cutoff, Article.published_at < cutoff),
                and_(Article.published_at.is_(None), Article.created_at >= previous_cutoff, Article.created_at < cutoff),
            )
        )
    ).all()

    def article_dt(a):
        return a.published_at or a.created_at

    # Daily volume series: one point for every calendar day in the selected range.
    daily = {}
    for i in range(days):
        d = (cutoff + timedelta(days=i)).date()
        daily[d.isoformat()] = 0
    for a, _sr in rows:
        dt = article_dt(a)
        if dt:
            key = dt.astimezone(timezone.utc).date().isoformat()
            if key in daily:
                daily[key] += 1

    source_counts = Counter(sr.name for _a, sr in rows)
    source_previous = Counter(sr.name for _a, sr in previous_rows)
    category_counts = Counter((a.category or "News") for a, _sr in rows)
    language_counts = Counter((a.language or "Unknown") for a, _sr in rows)
    coverage_counts = Counter()
    for _a, sr in rows:
        coverage = (sr.coverage or "Unknown").strip() or "Unknown"
        # Preserve configured coverage wording while keeping the executive chart concise.
        coverage_counts[coverage] += 1

    # Topic Velocity is based on document-level topic mentions rather than raw word
    # frequency. Compare the selected period with the immediately preceding period
    # of equal length so 7D/30D/90D views remain comparable.
    topics = _intel_topic_candidates(rows, previous_rows, limit=15)

    source_compare = []
    for name, count in source_counts.most_common(15):
        prev = source_previous.get(name, 0)
        delta = round(((count - prev) / prev) * 100, 1) if prev else (100.0 if count else 0.0)
        source_compare.append({"name": name, "count": count, "previous_count": prev, "change_pct": delta})

    # Build a compact cross-source story signal from the existing clustering table.
    clusters = db.scalars(
        select(StoryCluster)
        .order_by(StoryCluster.article_count.desc(), StoryCluster.last_updated_at.desc())
        .limit(12)
    ).all()
    story_clusters = [
        {
            "id": c.id,
            "title": c.title,
            "summary": c.summary,
            "article_count": c.article_count,
            "keywords": c.keywords,
            "last_updated_at": c.last_updated_at,
        }
        for c in clusters
    ]

    return {
        "status": "success",
        "user_key": user_key,
        "is_subscriber": bool(_subscriber_from_token(x_auth_token, db)),
        "days": days,
        "period": {
            "from": cutoff.isoformat(),
            "to": now.isoformat(),
            "previous_from": previous_cutoff.isoformat(),
            "previous_to": cutoff.isoformat(),
        },
        "summary": {
            "articles": len(rows),
            "previous_articles": len(previous_rows),
            "sources": len(source_counts),
            "categories": len(category_counts),
            "languages": len(language_counts),
        },
        "daily": [{"date": k, "articles": v} for k, v in daily.items()],
        "sources": source_compare,
        "topics": topics,
        "categories": [{"name": k, "count": v} for k, v in category_counts.most_common(10)],
        "languages": [{"name": k, "count": v} for k, v in language_counts.most_common(10)],
        "geography": [{"name": k, "count": v} for k, v in coverage_counts.most_common(12)],
        "story_clusters": story_clusters,
    }


@app.get("/api/intelligence/story-insight/{article_id}")
def intelligence_story_insight(
    article_id: int,
    x_user_key: str = Header(alias="X-User-Key"),
    x_auth_token: Optional[str] = Header(None, alias="X-Auth-Token"),
    db: Session = Depends(get_db),
):
    """Explain why a selected indexed story is significant using observable coverage signals."""
    user_key = _require_subscriber_or_free_user(x_user_key, x_auth_token, db)
    row = db.execute(
        select(Article, Source).join(Source, Article.source_id == Source.id).where(Article.id == article_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    article, source = row
    text = (article.title or "") + " " + (article.summary or "")
    keywords = [x for x in _intel_tokens(text) if len(x) >= 4][:8]
    related = []
    if keywords:
        predicates = [Article.title.ilike(f"%{k}%") for k in keywords[:5]]
        related_rows = db.execute(
            select(Article, Source)
            .join(Source, Article.source_id == Source.id)
            .where(Article.id != article.id, or_(*predicates))
            .order_by(Article.published_at.desc().nullslast(), Article.created_at.desc())
            .limit(12)
        ).all()
        related = [_intel_article_payload(a, sr) for a, sr in related_rows]

    category = article.category or "News"
    source_name = source.name or "Unknown Source"
    source_related = db.scalar(
        select(func.count()).select_from(Article).where(Article.source_id == source.id)
    ) or 0
    category_related = db.scalar(
        select(func.count()).select_from(Article).where(Article.category == category)
    ) or 0
    cross_sources = len({str(x.get("source")) for x in related if x.get("source")})
    why = []
    why.append(f"Published by {source_name} in the {category} category.")
    if related:
        why.append(f"Related indexed coverage was found across {cross_sources or 1} source(s), indicating that the topic is being reported beyond the selected story.")
    if source_related:
        why.append(f"The source currently contributes {source_related:,} indexed article(s) to the platform.")
    if category_related:
        why.append(f"The {category} category contains {category_related:,} indexed article(s), providing context for this story within the wider coverage set.")

    return {
        "status": "success",
        "user_key": user_key,
        "article": _intel_article_payload(article, source),
        "why_this_matters": why,
        "signals": {
            "related_articles": len(related),
            "cross_source_count": cross_sources,
            "source_article_count": source_related,
            "category_article_count": category_related,
        },
        "related": related,
    }

@app.post("/api/intelligence/rebuild")
def intelligence_rebuild(_: None=Depends(require_admin), db: Session=Depends(get_db)):
    return {"status":"success",**rebuild_story_clusters(db)}

@app.get("/api/intelligence/clusters")
def intelligence_clusters(limit:int=Query(30,ge=1,le=100), db:Session=Depends(get_db)):
    clusters=db.scalars(select(StoryCluster).order_by(StoryCluster.article_count.desc(),StoryCluster.last_updated_at.desc()).limit(limit)).all()
    return [{"id":c.id,"title":c.title,"summary":c.summary,"article_count":c.article_count,"keywords":c.keywords,"last_updated_at":c.last_updated_at} for c in clusters]

@app.get("/api/intelligence/clusters/{cluster_id}")
def intelligence_cluster_detail(cluster_id:int, db:Session=Depends(get_db)):
    c=db.get(StoryCluster,cluster_id)
    if not c: raise HTTPException(404,"Story cluster not found")
    rows=db.execute(select(Article,Source).join(Source,Article.source_id==Source.id).join(StoryClusterArticle,StoryClusterArticle.article_id==Article.id).where(StoryClusterArticle.cluster_id==cluster_id).order_by(Article.published_at.desc().nullslast())).all()
    return {"id":c.id,"title":c.title,"summary":c.summary,"keywords":c.keywords,"article_count":c.article_count,"articles":[_intel_article_payload(a,sr) for a,sr in rows]}

class TopicPayload(BaseModel):
    name:str
    query:str
class SavedSearchPayload(BaseModel):
    name:str
    query:str
    filters:dict={}
class AlertPayload(BaseModel):
    name:str
    query:str
    active:bool=True

@app.get("/api/me/topics")
def list_topics(x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db)
    return [{"id":t.id,"name":t.name,"query":t.query,"active":t.active,"created_at":t.created_at} for t in db.scalars(select(TrackedTopic).where(TrackedTopic.user_key==user).order_by(TrackedTopic.id.desc())).all()]

@app.post("/api/me/topics")
def create_topic(p:TopicPayload,x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db); t=TrackedTopic(user_key=user,name=p.name.strip(),query=p.query.strip()); db.add(t); db.commit(); db.refresh(t); return {"id":t.id,"name":t.name,"query":t.query,"active":t.active}

@app.delete("/api/me/topics/{topic_id}")
def delete_topic(topic_id:int,x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db); t=db.scalar(select(TrackedTopic).where(TrackedTopic.id==topic_id,TrackedTopic.user_key==user));
    if not t: raise HTTPException(404,"Topic not found")
    db.delete(t); db.commit(); return {"status":"success"}

@app.get("/api/me/saved-searches")
def list_saved(x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_paid_subscriber(x_user_key,x_auth_token,db).user_key; return [{"id":x.id,"name":x.name,"query":x.query,"filters":json.loads(x.filters_json or "{}"),"created_at":x.created_at} for x in db.scalars(select(SavedSearch).where(SavedSearch.user_key==user).order_by(SavedSearch.id.desc())).all()]

@app.post("/api/me/saved-searches")
def create_saved(p:SavedSearchPayload,x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_paid_subscriber(x_user_key,x_auth_token,db).user_key
    name=(p.name or "").strip()
    query=(p.query or "").strip()
    if not name or not query:
        raise HTTPException(status_code=422, detail="Search name and query are required.")
    try:
        x=SavedSearch(user_key=user,name=name[:255],query=query[:500],filters_json=json.dumps(p.filters or {}))
        db.add(x)
        db.commit()
        db.refresh(x)
        return {"id":x.id,"name":x.name,"query":x.query,"filters":p.filters or {}}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Unable to save search: {exc}")

@app.delete("/api/me/saved-searches/{item_id}")
def delete_saved(item_id:int,x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_paid_subscriber(x_user_key,x_auth_token,db).user_key; x=db.scalar(select(SavedSearch).where(SavedSearch.id==item_id,SavedSearch.user_key==user));
    if not x: raise HTTPException(404,"Saved search not found")
    db.delete(x); db.commit(); return {"status":"success"}

@app.get("/api/me/alerts")
def list_alerts(x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db); return [{"id":x.id,"name":x.name,"query":x.query,"active":x.active,"last_triggered_at":x.last_triggered_at} for x in db.scalars(select(AlertRule).where(AlertRule.user_key==user).order_by(AlertRule.id.desc())).all()]

@app.post("/api/me/alerts")
def create_alert(p:AlertPayload,x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db); x=AlertRule(user_key=user,name=p.name.strip(),query=p.query.strip(),active=p.active); db.add(x); db.commit(); db.refresh(x); return {"id":x.id,"name":x.name,"query":x.query,"active":x.active}

@app.delete("/api/me/alerts/{item_id}")
def delete_alert(item_id:int,x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db); x=db.scalar(select(AlertRule).where(AlertRule.id==item_id,AlertRule.user_key==user));
    if not x: raise HTTPException(404,"Alert not found")
    db.delete(x); db.commit(); return {"status":"success"}

@app.post("/api/me/briefing")
def generate_briefing(x_user_key:str=Header(alias="X-User-Key"),x_auth_token:Optional[str]=Header(None,alias="X-Auth-Token"),db:Session=Depends(get_db)):
    user=_require_subscriber_or_free_user(x_user_key,x_auth_token,db); date_key=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing=db.scalar(select(Briefing).where(Briefing.user_key==user,Briefing.briefing_date==date_key))
    if existing: return {"id":existing.id,"title":existing.title,"body":existing.body,"article_count":existing.article_count,"generated_at":existing.generated_at}
    cutoff=datetime.now(timezone.utc)-timedelta(hours=24)
    rows=db.execute(select(Article,Source).join(Source,Article.source_id==Source.id).where(Article.published_at>=cutoff).order_by(Article.published_at.desc()).limit(25)).all()
    lines=[]
    for i,(a,sr) in enumerate(rows[:10],1): lines.append(f"{i}. {a.title} — {sr.name}. {_intel_summary(a)}")
    body="\n".join(lines) if lines else "No new indexed articles were found in the last 24 hours."
    b=Briefing(user_key=user,briefing_date=date_key,title="Daily Media Intelligence Briefing",body=body,article_count=len(rows)); db.add(b); db.commit(); db.refresh(b); return {"id":b.id,"title":b.title,"body":b.body,"article_count":b.article_count,"generated_at":b.generated_at}


# =========================================================
# ADMIN — ePAPER AUTO-DISCOVERY
# =========================================================

@app.get("/api/admin/epaper/publishers")
def list_epaper_publishers(
    _: None = Depends(require_admin),
):
    return {
        "publishers": EPAPER_PUBLISHERS,
        "note": "Discovery collects official public edition links only. It does not download or archive ePaper content.",
    }


@app.post("/api/admin/epaper/discover")
def discover_epaper(
    payload: EpaperDiscoverRequest,
    _: None = Depends(require_admin),
):
    url = str(payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="ePaper URL is required.")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Enter a valid http/https ePaper URL.")

    publisher = next(
        (p for p in EPAPER_PUBLISHERS if payload.publisher_id and p["id"] == payload.publisher_id),
        None,
    )
    if publisher is None:
        publisher = {
            "id": payload.publisher_id or "custom",
            "name": payload.publisher or "Custom Publisher",
            "language": payload.language or "English",
            "coverage": "Custom",
        }

    try:
        final_url, rows = _discover_epaper_links(
            url,
            publisher,
            payload.language,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Publisher returned HTTP {exc.response.status_code} while opening the ePaper page.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"ePaper discovery failed: {str(exc)[:500]}",
        )

    return {
        "publisher": publisher,
        "source_url": final_url,
        "count": len(rows),
        "results": rows,
    }


@app.get("/api/epaper")
def public_epaper_directory():
    """Public ePaper directory. No Adamas admin or subscriber authentication required.

    Only official publisher edition URLs previously approved/saved by an administrator
    are exposed. The application does not proxy, download, or redistribute newspaper content.
    """
    rows = _epaper_json_read()
    public_rows = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        public_rows.append({
            "id": row.get("id"),
            "publisher": row.get("publisher") or "Publisher",
            "edition": row.get("edition") or "Edition",
            "date": row.get("date"),
            "language": row.get("language"),
            "url": url,
            "source_url": row.get("source_url"),
        })
    return {"results": public_rows, "count": len(public_rows)}


@app.get("/api/admin/epaper/saved")
def list_saved_epapers(
    _: None = Depends(require_admin),
):
    return {"results": _epaper_json_read()}


@app.post("/api/admin/epaper/saved")
def save_epaper(
    payload: EpaperSaveRequest,
    _: None = Depends(require_admin),
):
    url = str(payload.url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="A valid official ePaper URL is required.")

    rows = _epaper_json_read()
    if any(str(row.get("url")) == url for row in rows):
        return {"status": "exists", "results": rows}

    row = {
        "id": secrets.token_hex(8),
        "publisher": payload.publisher.strip()[:160],
        "edition": payload.edition.strip()[:160],
        "date": (payload.date or "").strip()[:80] or None,
        "language": (payload.language or "").strip()[:80] or None,
        "url": url[:2000],
        "source_url": (payload.source_url or "").strip()[:2000] or None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    rows.insert(0, row)
    _epaper_json_write(rows[:500])
    return {"status": "saved", "item": row, "results": rows[:500]}


@app.delete("/api/admin/epaper/saved/{item_id}")
def delete_saved_epaper(
    item_id: str,
    _: None = Depends(require_admin),
):
    rows = _epaper_json_read()
    new_rows = [row for row in rows if str(row.get("id")) != str(item_id)]
    if len(new_rows) == len(rows):
        raise HTTPException(status_code=404, detail="Saved ePaper entry not found.")
    _epaper_json_write(new_rows)
    return {"status": "deleted", "results": new_rows}


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get("/api/admin/dashboard")
def admin_dashboard(

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    total_sources = db.scalar(

        select(func.count())

        .select_from(
            Source
        )

    )

    active_sources = db.scalar(

        select(func.count())

        .select_from(
            Source
        )

        .where(
            Source.active == True
        )

    )

    total_feeds = db.scalar(

        select(func.count())

        .select_from(
            SourceFeed
        )

    )

    active_feeds = db.scalar(

        select(func.count())

        .select_from(
            SourceFeed
        )

        .where(
            SourceFeed.active == True
        )

    )

    total_articles = db.scalar(

        select(func.count())

        .select_from(
            Article
        )

    )

    articles_with_images = db.scalar(

        select(func.count())

        .select_from(
            Article
        )

        .where(

            Article.image_url.is_not(
                None
            )

        )

    )

    total_users = db.scalar(

        select(func.count())

        .select_from(
            Usage
        )

    )

    total_searches = db.scalar(

        select(

            func.coalesce(

                func.sum(
                    Usage.searches_used
                ),

                0,

            )

        )

    )

    return {

        "total_sources":
        total_sources,

        "active_sources":
        active_sources,

        "total_feeds":
        total_feeds,

        "active_feeds":
        active_feeds,

        "total_articles":
        total_articles,

        "articles_with_images":
        articles_with_images,

        "total_users":
        total_users,

        "total_searches":
        total_searches,

    }


# =========================================================
# ADMIN SOURCE LIST
# =========================================================

@app.get("/api/admin/sources")
def list_sources(

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    sources = db.scalars(

        select(Source)

        .order_by(
            Source.id
        )

    ).all()

    result = []

    for source in sources:

        article_count = db.scalar(

            select(func.count())

            .select_from(
                Article
            )

            .where(

                Article.source_id
                == source.id

            )

        )

        result.append({

            "id":
            source.id,

            "source_code":
            source.source_code,

            "name":
            source.name,

            "website":
            source.website,

            "rss_url":
            source.rss_url,

            "language":
            source.language,

            "coverage":
            source.coverage,

            "active":
            source.active,

            "decision":
            source.decision,

            "last_fetch_at":
            source.last_fetch_at,

            "last_error":
            source.last_error,

            "article_count":
            article_count,

        })

    return result


# =========================================================
# SOURCE TEMPLATE / BULK IMPORT
# =========================================================

@app.get("/api/admin/sources/template")
def download_source_template(

    _: None = Depends(require_admin),

):

    csv_text = (
        "source_code,name,website,rss_url,language,coverage,active,decision\n"
        "EXAMPLE,Example News,https://example.com,https://example.com/rss,English,India,TRUE,HOLD\n"
    )

    return Response(
        content="\ufeff" + csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="source_management_template.csv"'
        },
    )


@app.post("/api/admin/sources/bulk-import")
async def bulk_import_sources(

    file: UploadFile = File(...),

    _: None = Depends(require_admin),

    db: Session = Depends(get_db),

):

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    reader = csv.DictReader(text.splitlines())
    required = {"source_code", "name", "website"}
    headers = {str(h or "").strip() for h in (reader.fieldnames or [])}
    if not required.issubset(headers):
        raise HTTPException(400, detail={
            "message": "Invalid source CSV columns. Required: source_code, name, website.",
            "required_columns": sorted(required),
        })

    processed = created = skipped = 0
    errors = []

    def parse_bool(value):
        v = str(value or "").strip().lower()
        if v in {"true", "yes", "1", "active"}: return True
        if v in {"false", "no", "0", "inactive"}: return False
        return None

    for row_no, row in enumerate(reader, start=2):
        processed += 1
        row = {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
        code = row.get("source_code", "").strip()
        name = row.get("name", "").strip()
        website = row.get("website", "").strip()
        if not code or not name or not website:
            errors.append({"row": row_no, "message": "source_code, name and website are required."})
            continue
        if db.scalar(select(Source).where(Source.source_code == code)):
            skipped += 1
            continue
        active = parse_bool(row.get("active", "false"))
        if active is None:
            errors.append({"row": row_no, "message": "Invalid active value; use TRUE/FALSE, YES/NO, 1/0 or ACTIVE/INACTIVE."})
            continue
        db.add(Source(
            source_code=code,
            name=name,
            website=website,
            rss_url=row.get("rss_url") or None,
            language=row.get("language") or "English",
            coverage=row.get("coverage") or "India",
            active=active,
            decision=row.get("decision") or "HOLD",
        ))
        created += 1

    db.commit()
    return {"status": "success", "processed": processed, "created": created, "skipped_duplicates": skipped, "errors": errors}


# =========================================================
# ADD SOURCE
# =========================================================

@app.post("/api/admin/sources")
def add_source(

    item: SourceIn,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    existing = db.scalar(

        select(Source)

        .where(

            Source.source_code
            == item.source_code

        )

    )

    if existing:

        raise HTTPException(

            status_code=409,

            detail=
            "Source code already exists",

        )

    source = Source(
        **item.model_dump()
    )

    db.add(source)

    db.commit()

    db.refresh(source)

    return {

        "id":
        source.id,

        "status":
        "created",

    }



# =========================================================
# UPDATE SOURCE
# =========================================================

@app.put("/api/admin/sources/{source_id}")
def update_source(

    source_id: int,

    item: SourceIn,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    source = db.get(
        Source,
        source_id,
    )

    if not source:

        raise HTTPException(

            status_code=404,

            detail="Source not found",

        )

    duplicate = db.scalar(

        select(Source)

        .where(
            Source.source_code
            == item.source_code
        )

        .where(
            Source.id
            != source_id
        )

    )

    if duplicate:

        raise HTTPException(

            status_code=409,

            detail="Source code already belongs to another source",

        )

    source.source_code = item.source_code
    source.name = item.name
    source.website = item.website
    source.rss_url = item.rss_url
    source.language = item.language
    source.coverage = item.coverage
    source.active = item.active
    source.decision = item.decision

    db.commit()

    db.refresh(source)

    return {

        "id": source.id,

        "status": "updated",

        "source_code": source.source_code,

        "name": source.name,

        "active": source.active,

        "decision": source.decision,

    }


# =========================================================
# DELETE SOURCE
# =========================================================

@app.delete("/api/admin/sources/{source_id}")
def delete_source(

    source_id: int,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    source = db.get(
        Source,
        source_id,
    )

    if not source:

        raise HTTPException(

            status_code=404,

            detail="Source not found",

        )

    feed_count = db.scalar(

        select(func.count())

        .select_from(
            SourceFeed
        )

        .where(
            SourceFeed.source_id
            == source_id
        )

    ) or 0

    article_count = db.scalar(

        select(func.count())

        .select_from(
            Article
        )

        .where(
            Article.source_id
            == source_id
        )

    ) or 0

    if feed_count > 0 or article_count > 0:

        raise HTTPException(

            status_code=409,

            detail=(
                "This source cannot be deleted because it has "
                f"{feed_count} feed(s) and {article_count} article(s). "
                "Deactivate it instead."
            ),

        )

    db.delete(source)

    db.commit()

    return {

        "id": source_id,

        "status": "deleted",

    }



# =========================================================
# TOGGLE SOURCE
# =========================================================

@app.post(
    "/api/admin/sources/{source_id}/toggle"
)
def toggle_source(

    source_id: int,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    source = db.get(
        Source,
        source_id,
    )

    if not source:

        raise HTTPException(

            status_code=404,

            detail=
            "Source not found",

        )

    source.active = (
        not source.active
    )

    db.commit()

    return {

        "id":
        source.id,

        "active":
        source.active,

    }


# =========================================================
# ADMIN FEED LIST
# =========================================================

@app.get("/api/admin/feeds")
def list_feeds(

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    rows = db.execute(

        select(

            SourceFeed,
            Source,

        )

        .join(

            Source,

            SourceFeed.source_id
            == Source.id,

        )

        .order_by(
            SourceFeed.id
        )

    ).all()

    return [

        {

            "id":
            feed.id,

            "source_id":
            feed.source_id,

            "source":
            source.name,

            "feed_name":
            feed.feed_name,

            "feed_url":
            feed.feed_url,

            "category":
            feed.category,

            "language":
            feed.language,

            "active":
            feed.active,

            "decision":
            feed.decision,

            "rss_verified":
            feed.rss_verified,

            "last_test_at":
            feed.last_test_at,

            "last_fetch_at":
            feed.last_fetch_at,

            "last_error":
            feed.last_error,

        }

        for feed, source
        in rows

    ]


# =========================================================
# RSS FEED TEMPLATE / BULK IMPORT
# =========================================================

@app.get("/api/admin/feeds/template")
def download_feed_template(

    _: None = Depends(require_admin),

):

    csv_text = (
        "source_code,feed_name,feed_url,category,language,active,decision\n"
        "EXAMPLE,Example RSS,https://example.com/rss,National,English,TRUE,HOLD\n"
    )

    return Response(
        content="\ufeff" + csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="rss_feed_management_template.csv"'
        },
    )


@app.post("/api/admin/feeds/bulk-import")
async def bulk_import_feeds(

    file: UploadFile = File(...),

    _: None = Depends(require_admin),

    db: Session = Depends(get_db),

):

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    reader = csv.DictReader(text.splitlines())
    required = {"source_code", "feed_name", "feed_url"}
    headers = {str(h or "").strip() for h in (reader.fieldnames or [])}
    if not required.issubset(headers):
        raise HTTPException(400, detail={
            "message": "Invalid RSS feed CSV columns. Required: source_code, feed_name, feed_url.",
            "required_columns": sorted(required),
        })

    processed = created = skipped = 0
    errors = []

    def parse_bool(value):
        v = str(value or "").strip().lower()
        if v in {"true", "yes", "1", "active"}: return True
        if v in {"false", "no", "0", "inactive"}: return False
        return None

    source_map = {
        str(code).strip(): source
        for source in db.scalars(select(Source)).all()
        for code in [source.source_code]
    }

    for row_no, row in enumerate(reader, start=2):
        processed += 1
        row = {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
        code = row.get("source_code", "").strip()
        feed_name = row.get("feed_name", "").strip()
        feed_url = row.get("feed_url", "").strip()
        if not code or not feed_name or not feed_url:
            errors.append({"row": row_no, "message": "source_code, feed_name and feed_url are required."})
            continue
        source = source_map.get(code)
        if not source:
            errors.append({"row": row_no, "message": f"Source code '{code}' was not found."})
            continue
        if db.scalar(select(SourceFeed).where(SourceFeed.feed_url == feed_url)):
            skipped += 1
            continue
        active = parse_bool(row.get("active", "false"))
        if active is None:
            errors.append({"row": row_no, "message": "Invalid active value; use TRUE/FALSE, YES/NO, 1/0 or ACTIVE/INACTIVE."})
            continue
        db.add(SourceFeed(
            source_id=source.id,
            feed_name=feed_name,
            feed_url=feed_url,
            category=row.get("category") or None,
            language=row.get("language") or "English",
            active=active,
            decision=row.get("decision") or "HOLD",
        ))
        created += 1

    db.commit()
    return {"status": "success", "processed": processed, "created": created, "skipped_duplicates": skipped, "errors": errors}


# =========================================================
# ADD FEED
# =========================================================

@app.post("/api/admin/feeds")
def add_feed(

    item: FeedIn,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    source = db.get(
        Source,
        item.source_id,
    )

    if not source:

        raise HTTPException(

            status_code=404,

            detail=
            "Source not found",

        )

    existing = db.scalar(

        select(SourceFeed)

        .where(

            SourceFeed.feed_url
            == item.feed_url

        )

    )

    if existing:

        raise HTTPException(

            status_code=409,

            detail=
            "Feed already exists",

        )

    feed = SourceFeed(
        **item.model_dump()
    )

    db.add(feed)

    db.commit()

    db.refresh(feed)

    return {

        "id":
        feed.id,

        "status":
        "created",

    }


# =========================================================
# TOGGLE FEED
# =========================================================

@app.post(
    "/api/admin/feeds/{feed_id}/toggle"
)
def toggle_feed(

    feed_id: int,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    feed = db.get(
        SourceFeed,
        feed_id,
    )

    if not feed:

        raise HTTPException(

            status_code=404,

            detail=
            "Feed not found",

        )

    feed.active = (
        not feed.active
    )

    db.commit()

    return {

        "id":
        feed.id,

        "active":
        feed.active,

    }


# =========================================================
# TEST RSS FEED
# =========================================================

@app.post(
    "/api/admin/feeds/{feed_id}/test"
)
async def test_feed(

    feed_id: int,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    feed = db.get(
        SourceFeed,
        feed_id,
    )

    if not feed:

        raise HTTPException(

            status_code=404,

            detail=
            "Feed not found",

        )

    try:

        async with httpx.AsyncClient(

            timeout=30,

            follow_redirects=True,

            headers={

                "User-Agent":
                "AdamasMediaIntelligence/0.5",

            },

        ) as client:

            response = await client.get(
                feed.feed_url
            )

            response.raise_for_status()

        parsed = feedparser.parse(
            response.content
        )

        entries_found = len(
            parsed.entries
        )

        feed.rss_verified = (
            entries_found > 0
        )

        feed.last_test_at = (
            datetime.now(
                timezone.utc
            )
        )

        feed.last_error = None

        db.commit()

        samples = []

        for entry in parsed.entries[:5]:

            image_url = extract_entry_image(
                entry
            )

            samples.append({

                "title":
                getattr(
                    entry,
                    "title",
                    None,
                ),

                "url":
                getattr(
                    entry,
                    "link",
                    None,
                ),

                "image_url":
                image_url,

            })

        return {

            "status":
            "success",

            "source_feed":
            feed.feed_name,

            "feed_title":
            getattr(
                parsed.feed,
                "title",
                None,
            ),

            "entries_found":
            entries_found,

            "sample_entries":
            samples,

        }

    except Exception as exc:

        feed.rss_verified = False

        feed.last_test_at = (

            datetime.now(
                timezone.utc
            )

        )

        feed.last_error = str(exc)

        db.commit()

        raise HTTPException(

            status_code=400,

            detail={

                "message":
                "Feed test failed",

                "error":
                str(exc),

            },

        )


# =========================================================
# INGEST RSS FEED
# =========================================================

@app.post(
    "/api/admin/feeds/{feed_id}/ingest"
)
async def ingest_feed(

    feed_id: int,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    feed = db.get(
        SourceFeed,
        feed_id,
    )

    if not feed:

        raise HTTPException(

            status_code=404,

            detail=
            "Feed not found",

        )

    if not feed.active:

        raise HTTPException(

            status_code=400,

            detail=
            "Feed is not active",

        )

    source = db.get(
        Source,
        feed.source_id,
    )

    if not source:

        raise HTTPException(

            status_code=404,

            detail=
            "Source not found",

        )

    try:

        async with httpx.AsyncClient(

            timeout=30,

            follow_redirects=True,

            headers={

                "User-Agent":
                "AdamasMediaIntelligence/0.5",

            },

        ) as client:

            response = await client.get(
                feed.feed_url
            )

            response.raise_for_status()

        parsed = feedparser.parse(
            response.content
        )

        added = 0

        skipped = 0

        images_updated = 0

        for entry in parsed.entries:

            url = getattr(
                entry,
                "link",
                None,
            )

            title = getattr(
                entry,
                "title",
                None,
            )

            if not url or not title:

                skipped += 1

                continue

            summary = getattr(
                entry,
                "summary",
                None,
            )

            image_url = extract_entry_image(
                entry
            )

            content_text = (

                f"{title}|"

                f"{summary or ''}|"

                f"{url}"

            )

            content_hash = (

                hashlib.sha256(

                    content_text.encode(
                        "utf-8"
                    )

                )

                .hexdigest()

            )

            existing = db.scalar(

                select(Article)

                .where(

                    or_(

                        Article.url
                        == url,

                        Article.content_hash
                        == content_hash,

                    )

                )

            )

            # Existing article
            if existing:

                # Update image if previously empty
                if (

                    not existing.image_url

                    and image_url

                ):

                    existing.image_url = (
                        image_url[:2000]
                    )

                    images_updated += 1

                skipped += 1

                continue

            published = None

            if getattr(

                entry,

                "published_parsed",

                None,

            ):

                published = (

                    datetime.fromtimestamp(

                        calendar.timegm(

                            entry.published_parsed

                        ),

                        tz=timezone.utc,

                    )

                )

            article = Article(

                source_id=
                source.id,

                title=
                title[:1000],

                url=
                url[:1500],

                content_hash=
                content_hash,

                image_url=(

                    image_url[:2000]

                    if image_url

                    else None

                ),

                summary=
                summary,

                category=(

                    feed.category

                    or "News"

                ),

                language=(

                    feed.language

                    or source.language

                ),

                published_at=
                published,

            )

            db.add(article)

            added += 1

        now = datetime.now(
            timezone.utc
        )

        feed.last_fetch_at = now

        feed.last_error = None

        source.last_fetch_at = now

        source.last_error = None

        db.commit()

        return {

            "status":
            "success",

            "source":
            source.name,

            "feed":
            feed.feed_name,

            "added":
            added,

            "skipped":
            skipped,

            "images_updated":
            images_updated,

        }

    except Exception as exc:

        feed.last_error = str(exc)

        source.last_error = str(exc)

        db.commit()

        raise HTTPException(

            status_code=400,

            detail={

                "message":
                "Feed ingestion failed",

                "error":
                str(exc),

            },

        )



# =========================================================
# UPDATE RSS FEED
# =========================================================

@app.put("/api/admin/feeds/{feed_id}")
def update_feed(
    feed_id: int,
    item: FeedIn,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):

    feed = db.get(
        SourceFeed,
        feed_id,
    )

    if not feed:

        raise HTTPException(
            status_code=404,
            detail="Feed not found",
        )

    source = db.get(
        Source,
        item.source_id,
    )

    if not source:

        raise HTTPException(
            status_code=404,
            detail="Source not found",
        )

    duplicate = db.scalar(
        select(SourceFeed)
        .where(
            SourceFeed.feed_url
            == item.feed_url
        )
        .where(
            SourceFeed.id
            != feed_id
        )
    )

    if duplicate:

        raise HTTPException(
            status_code=409,
            detail="Feed URL already belongs to another feed",
        )

    previous_url = feed.feed_url

    feed.source_id = item.source_id
    feed.feed_name = item.feed_name
    feed.feed_url = item.feed_url
    feed.category = item.category
    feed.language = item.language
    feed.active = item.active
    feed.decision = item.decision

    # A changed URL must be tested again.
    if previous_url != item.feed_url:
        feed.rss_verified = False
        feed.last_test_at = None
        feed.last_error = None

    db.commit()
    db.refresh(feed)

    return {
        "id": feed.id,
        "status": "updated",
        "source_id": feed.source_id,
        "feed_name": feed.feed_name,
        "feed_url": feed.feed_url,
        "active": feed.active,
        "decision": feed.decision,
        "rss_verified": feed.rss_verified,
    }


# =========================================================
# DELETE RSS FEED
# =========================================================

@app.delete("/api/admin/feeds/{feed_id}")
def delete_feed(
    feed_id: int,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):

    feed = db.get(
        SourceFeed,
        feed_id,
    )

    if not feed:

        raise HTTPException(
            status_code=404,
            detail="Feed not found",
        )

    db.delete(feed)
    db.commit()

    return {
        "id": feed_id,
        "status": "deleted",
    }


# =========================================================
# GET ONE RSS FEED
# =========================================================

@app.get("/api/admin/feeds/{feed_id}")
def get_feed(
    feed_id: int,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):

    feed = db.get(
        SourceFeed,
        feed_id,
    )

    if not feed:

        raise HTTPException(
            status_code=404,
            detail="Feed not found",
        )

    source = db.get(
        Source,
        feed.source_id,
    )

    return {
        "id": feed.id,
        "source_id": feed.source_id,
        "source": source.name if source else None,
        "feed_name": feed.feed_name,
        "feed_url": feed.feed_url,
        "category": feed.category,
        "language": feed.language,
        "active": feed.active,
        "decision": feed.decision,
        "rss_verified": feed.rss_verified,
        "last_test_at": feed.last_test_at,
        "last_fetch_at": feed.last_fetch_at,
        "last_error": feed.last_error,
        "created_at": feed.created_at,
    }

# =========================================================
# ADMIN — AUTOMATIC GLOBAL RSS DISCOVERY
# =========================================================

@app.get("/api/admin/settings/auto-rss-discovery")
def get_auto_rss_discovery_setting(
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {
        "enabled": get_app_bool(db, AUTO_RSS_DISCOVERY_KEY, False),
        "interval_minutes": get_app_int(db, AUTO_RSS_DISCOVERY_INTERVAL_KEY, AUTO_RSS_DISCOVERY_DEFAULT_INTERVAL),
        "description": "Discover publisher RSS/Atom feeds from global news results and publisher RSS autodiscovery.",
    }


@app.put("/api/admin/settings/auto-rss-discovery")
def update_auto_rss_discovery_setting(
    payload: dict,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    enabled = bool(payload.get("enabled", False))
    interval = int(payload.get("interval_minutes", AUTO_RSS_DISCOVERY_DEFAULT_INTERVAL))
    if interval < 60 or interval > 10080:
        raise HTTPException(status_code=400, detail="Discovery interval must be between 60 minutes and 7 days.")
    set_app_setting(db, AUTO_RSS_DISCOVERY_KEY, "true" if enabled else "false")
    set_app_setting(db, AUTO_RSS_DISCOVERY_INTERVAL_KEY, str(interval))
    db.commit()
    return {"status": "success", "enabled": enabled, "interval_minutes": interval}


@app.post("/api/admin/settings/auto-rss-discovery/run-now")
async def run_auto_rss_discovery_now(_: None = Depends(require_admin)):
    global manual_discovery_task
    if manual_discovery_task is not None and not manual_discovery_task.done():
        return {"status": "running", **manual_discovery_status}
    manual_discovery_task = asyncio.create_task(_run_discovery_job())
    return {"status": "started", **manual_discovery_status}


@app.get("/api/admin/settings/auto-rss-discovery/status")
def get_auto_rss_discovery_status(_: None = Depends(require_admin)):
    return {"status": "running" if manual_discovery_status.get("running") else manual_discovery_status.get("stage", "idle"), **manual_discovery_status}

# =========================================================
# ADMIN USERS
# =========================================================

@app.get("/api/admin/settings/search-quota")
def get_search_quota_setting(
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {
        "status": "success",
        "free_search_limit": get_free_search_limit(db),
        "default_from_environment": FREE_SEARCH_LIMIT,
    }


@app.put("/api/admin/settings/search-quota")
def update_search_quota_setting(
    payload: dict,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    raw_limit = payload.get("free_search_limit")
    try:
        free_search_limit = int(raw_limit)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Free search limit must be a whole number from 0 to 1000000.",
        )

    if free_search_limit < 0 or free_search_limit > 1_000_000:
        raise HTTPException(
            status_code=400,
            detail="Free search limit must be between 0 and 1000000.",
        )

    setting = db.scalar(
        select(AppSetting).where(AppSetting.setting_key == FREE_SEARCH_LIMIT_KEY)
    )
    if setting is None:
        setting = AppSetting(
            setting_key=FREE_SEARCH_LIMIT_KEY,
            setting_value=str(free_search_limit),
        )
        db.add(setting)
    else:
        setting.setting_value = str(free_search_limit)

    db.commit()
    db.refresh(setting)
    return {
        "status": "success",
        "message": "Free search limit updated successfully.",
        "free_search_limit": free_search_limit,
    }



# =========================================================
# ADMIN SUBSCRIBER MANAGEMENT
# =========================================================

@app.get("/api/admin/subscribers")
def list_subscribers(
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    accounts = db.scalars(
        select(SubscriberAccount).order_by(
            SubscriberAccount.id.desc()
        )
    ).all()

    return [
        {
            "id": account.id,
            "email": account.email,
            "user_key": account.user_key,
            "subscribed": account.subscribed,
            "active": account.active,
            "created_at": account.created_at,
            "last_login_at": account.last_login_at,
        }
        for account in accounts
    ]


@app.post("/api/admin/subscribers")
def create_subscriber(
    payload: AdminSubscriberCreateRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    email = payload.email.strip().lower()

    if "@" not in email:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid subscriber email address.",
        )

    if len(payload.password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Subscriber password must contain at least 8 characters.",
        )

    existing = db.scalar(
        select(SubscriberAccount).where(
            SubscriberAccount.email == email
        )
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail="A subscriber account with this email already exists.",
        )

    user_key = "sub-" + secrets.token_urlsafe(24)

    account = SubscriberAccount(
        email=email,
        password_hash=_password_hash(payload.password),
        user_key=user_key,
        subscribed=payload.subscribed,
        active=payload.active,
    )
    db.add(account)

    usage = Usage(
        user_key=user_key,
        searches_used=0,
        subscribed=payload.subscribed,
    )
    db.add(usage)
    db.commit()
    db.refresh(account)

    return {
        "status": "success",
        "message": "Subscriber account created.",
        "id": account.id,
        "email": account.email,
        "user_key": account.user_key,
        "subscribed": account.subscribed,
        "active": account.active,
    }


@app.put("/api/admin/subscribers/{subscriber_id}")
def update_subscriber_subscription(
    subscriber_id: int,
    payload: AdminSubscriptionUpdateRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(SubscriberAccount, subscriber_id)

    if not account:
        raise HTTPException(
            status_code=404,
            detail="Subscriber account not found.",
        )

    account.subscribed = payload.subscribed

    if payload.active is not None:
        account.active = payload.active

    if not account.subscribed or not account.active:
        account.auth_token = None
        account.token_expires_at = None

    usage = db.scalar(
        select(Usage).where(
            Usage.user_key == account.user_key
        )
    )
    if usage:
        usage.subscribed = False

    db.commit()
    db.refresh(account)

    return {
        "status": "success",
        "message": "Subscriber access updated.",
        "id": account.id,
        "email": account.email,
        "subscribed": account.subscribed,
        "active": account.active,
    }


@app.get("/api/admin/users")
def list_users(

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    users = db.scalars(

        select(Usage)

        .order_by(
            Usage.id.desc()
        )

    ).all()

    result = []

    for user in users:

        remaining = max(

            0,

            get_free_search_limit(db)
            - user.searches_used,

        )

        result.append({

            "id":
            user.id,

            "user_key":
            user.user_key,

            "searches_used":
            user.searches_used,

            "free_limit":
            get_free_search_limit(db),

            "remaining":

            None

            if user.subscribed

            else remaining,

            "subscribed":
            user.subscribed,

        })

    return result


# =========================================================
# ADMIN RESET ONE USER QUOTA
# =========================================================

@app.post(
    "/api/admin/users/{user_id}/reset"
)
def reset_user_searches(

    user_id: int,

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    user = db.get(
        Usage,
        user_id,
    )

    if not user:

        raise HTTPException(

            status_code=404,

            detail=
            "User not found",

        )

    previous_searches_used = (
        user.searches_used
    )

    user.searches_used = 0

    db.commit()

    db.refresh(user)

    return {

        "status":
        "success",

        "message":
        "User search quota reset successfully.",

        "user_id":
        user.id,

        "user_key":
        user.user_key,

        "previous_searches_used":
        previous_searches_used,

        "searches_used":
        user.searches_used,

        "free_limit":
        get_free_search_limit(db),

        "remaining":
        get_free_search_limit(db),

    }


# =========================================================
# ADMIN RESET ALL USER QUOTAS
# =========================================================

@app.post(
    "/api/admin/users/reset-all"
)
def reset_all_users(

    _: None = Depends(
        require_admin
    ),

    db: Session = Depends(
        get_db
    ),

):

    users = db.scalars(

        select(Usage)

    ).all()

    users_reset = 0

    for user in users:

        if user.searches_used != 0:

            user.searches_used = 0

            users_reset += 1

    db.commit()

    return {

        "status":
        "success",

        "message":
        "All user search quotas have been reset successfully.",

        "users_reset":
        users_reset,

        "free_limit":
        get_free_search_limit(db),

    }






