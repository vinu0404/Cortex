"""Standalone Scrapy runner — invoked as a subprocess from the Celery crawl task.

Positional args (sys.argv):
    1: start_url      — URL to crawl
    2: max_depth      — int, maximum link depth
    3: output_path    — file path for JSONL output
    4: max_pages      — int, page cap
    5: cfg_json       — JSON object with Scrapy settings

Exit codes:
    0: spider completed (output file written)
    1: argument / config error
    2: spider runtime error
"""
import json
import logging
import sys

logging.basicConfig(
    stream=sys.stderr,
    level=logging.ERROR,
    format="%(asctime)s [spider-runner] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) != 6:
        logger.error(
            "Expected 5 positional args, got %d. "
            "Usage: python -m web_pipeline.runner <url> <max_depth> "
            "<output_path> <max_pages> <cfg_json>",
            len(sys.argv) - 1,
        )
        sys.exit(1)

    try:
        url = sys.argv[1]
        max_depth = int(sys.argv[2])
        output_path = sys.argv[3]
        max_pages = int(sys.argv[4])
        cfg = json.loads(sys.argv[5])
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Invalid arguments: %s", exc)
        sys.exit(1)

    try:
        from scrapy.crawler import CrawlerProcess
        from web_pipeline.spider import CortexSpider
    except ImportError as exc:
        logger.error("Import error — scrapy or spider not available: %s", exc)
        sys.exit(2)

    try:
        process = CrawlerProcess({
            "ROBOTSTXT_OBEY": cfg.get("obey_robots", False),
            "USER_AGENT": cfg.get("user_agent", "CortexBot/1.0"),
            "CONCURRENT_REQUESTS": cfg.get("concurrent_requests", 4),
            "DOWNLOAD_TIMEOUT": cfg.get("download_timeout", 30),
            "DEPTH_LIMIT": max_depth,
            "LOG_ENABLED": True,
            "LOG_LEVEL": "INFO",
            "LOG_FILE": None,
            "LOG_STDOUT": False,
            "TELNETCONSOLE_ENABLED": False,
            "COOKIES_ENABLED": False,
        })
        process.crawl(
            CortexSpider,
            start_url=url,
            max_depth=max_depth,
            output_path=output_path,
            max_pages=max_pages,
        )
        process.start()
    except Exception as exc:
        logger.error("Spider failed: %s", exc, exc_info=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
