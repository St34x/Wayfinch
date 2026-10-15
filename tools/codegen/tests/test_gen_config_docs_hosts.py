# SPDX-License-Identifier: Apache-2.0
"""Host and URL checks of tools/codegen/gen_config_docs.py beyond test_gen_config_docs.py: every URL and host in the
committed doc and in the generator's examples names example.com or a subdomain of it, judged by the parsed host (never
a substring), and the checks reject hosts a browser could read differently.
"""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import gen_config_docs as gen
import pytest
import yaml

# tools/codegen is on sys.path through conftest.py.
ROOT = Path(__file__).resolve().parents[3]
COMMITTED = ROOT / gen.OUTPUT


def _example() -> Any:
    block = re.search(r"```yaml\n(.*?)```", COMMITTED.read_text(encoding="utf-8"), re.DOTALL)
    assert block is not None
    return yaml.safe_load(block[1])["gateway"]["platforms"]["glasses"]["extra"]


HOST_NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")
URL_TOKEN = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)  # whole URL tokens, any scheme case


def _urls(text: str) -> list[str]:
    return [token.rstrip("`'\").,;:|>") for token in URL_TOKEN.findall(text)]  # trailing Markdown/prose only


def _is_example_host(host: str | None) -> bool:
    return host is not None and (host == "example.com" or host.endswith(".example.com"))


def _host(url_or_host: str) -> str | None:
    """The parsed host of a URL or bare host (a substring test would pass `example.com.evil.net`); None for a
    backslash (WHATWG reads it as `/`, urlsplit does not) or a host that is not plain lower-case ASCII labels."""
    if "\\" in url_or_host:
        return None
    try:
        host = urlsplit(url_or_host if "://" in url_or_host else f"//{url_or_host}").hostname
    except ValueError:
        return None
    return host if host is not None and HOST_NAME.fullmatch(host) else None


def test_urls_and_hosts_use_example_com_only() -> None:
    urls = _urls(COMMITTED.read_text(encoding="utf-8"))
    hosts = urls + [str(_example()["meeting"]["endpoint"])]
    assert urls
    assert all(_is_example_host(_host(host)) for host in hosts), hosts
    url_or_host = [str(value) for value in gen.EXAMPLES.values() if not str(value).startswith("/")]
    assert url_or_host and all(_is_example_host(_host(value)) for value in url_or_host), url_or_host


def test_url_scrape_judges_the_real_host() -> None:
    urls = _urls("| `HTTPS://evil.net/x` | `wss://example.com\x40evil.net/ws` | wss:// URL | '://' |")
    assert [_host(url) for url in urls] == ["evil.net", "evil.net"]


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("wss://glasses.example.com/ws", True),
        ("stt.example.com", True),
        ("example.com:443", True),
        ("wss://example.com.evil.net/ws", False),
        ("wss://evil.net/?next=example.com", False),
        ("wss://example.com\x40evil.net/ws", False),
        ("notexample.com", False),
        (r"https://evil.net\.example.com/", False),
        (r"https://evil.net\@example.com/", False),
        ("https://EXAMPLE.COM/", True),
        ("HTTPS://evil.net/", False),
        ("https://evil.net%2f.example.com/", False),
    ],
)
def test_example_host_check_parses_the_host(value: str, ok: bool) -> None:
    assert _is_example_host(_host(value)) is ok
