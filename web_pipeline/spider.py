import json
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import CrawlSpider, Rule

_LOGIN_URL_PATTERNS = (
    "/login", "/signin", "/sign-in", "/auth", "/account/login",
    "?redirect=", "?next=", "/sso", "/oauth", "/session/new",
)
_LOGIN_TEXT_SIGNALS = (
    "sign in to", "please log in", "login required",
    "access denied", "you must be logged in",
    "create an account", "forgot your password",
)


def _is_login_wall(response) -> bool:
    url_lower = response.url.lower()
    if any(p in url_lower for p in _LOGIN_URL_PATTERNS):
        return True
    if response.status in (401, 403):
        return True
    try:
        body_lower = response.text[:3000].lower()
    except Exception:
        return False
    if '<input type="password"' in body_lower:
        return True
    if any(sig in body_lower for sig in _LOGIN_TEXT_SIGNALS):
        return True
    return False


class CortexSpider(CrawlSpider):
    name = "cortex_spider"
    rules = (Rule(LinkExtractor(deny_extensions=[]), callback="parse_page", follow=True),)

    def __init__(self, start_url: str, max_depth: int, output_path: str, max_pages: int, **kwargs):
        self.start_urls = [start_url]
        self.allowed_domains = [urlparse(start_url).netloc]
        self.max_depth = int(max_depth)
        self.max_pages = int(max_pages)
        self._page_count = 0
        self._seen_urls: set[str] = set()
        self._out = open(output_path, "w", encoding="utf-8")
        super().__init__(**kwargs)

    def parse_start_url(self, response, **kwargs):
        self.parse_page(response)
        return []

    def parse_page(self, response, **kwargs):
        normalized = response.url.rstrip("/")
        if normalized in self._seen_urls:
            return
        self._seen_urls.add(normalized)

        depth = response.meta.get("depth", 0)

        if _is_login_wall(response):
            is_start = response.url.rstrip("/") == self.start_urls[0].rstrip("/")
            self._out.write(json.dumps({
                "login_blocked": True,
                "url": response.url,
                "is_start_url": is_start,
            }) + "\n")
            self._out.flush()
            self._login_blocked += 1
            return  # don't follow links from login pages

        if depth > self.max_depth or self._page_count >= self.max_pages:
            return

        try:
            response.css("script, style, nav, header, footer").drop()
            text = " ".join(response.css("body *::text").getall())
            text = " ".join(text.split())
        except Exception:
            logger.warning("Text extraction failed for %s", response.url, exc_info=True)
            return

        if len(text) < 50:
            return

        title = (
            response.css("title::text").get("")
            or response.css("h1::text").get("")
            or response.url
        )
        self._out.write(json.dumps({
            "url": response.url,
            "title": title.strip(),
            "text": text[:50_000],
            "depth": depth,
        }) + "\n")
        self._out.flush()
        self._page_count += 1

    def closed(self, reason):
        self._out.close()
