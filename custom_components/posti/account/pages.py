"""Picking the few values we need out of Posti's login pages.

Nothing else on the pages is read, and anything unexpected yields nothing
rather than a guess.
"""

from __future__ import annotations

from html.parser import HTMLParser
import re
import urllib.parse

_METHOD_LINK = re.compile(r"/uas/authn/[^\s\"'<>]*?/select\?[^\s\"'<>]*")
_BANK_HOST = "tunnistus.telia.fi"


class _Scan(HTMLParser):
    """Collect the hidden inputs and the form targets of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.hidden: dict[str, str] = {}
        self.form_targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "input" and "name" in values and "value" in values:
            self.hidden.setdefault(values["name"], values["value"])
        elif tag == "form" and values.get("action"):
            self.form_targets.append(values["action"])


def _scan(page: str) -> _Scan:
    scan = _Scan()
    scan.feed(page)
    return scan


def hidden_value(page: str, name: str) -> str | None:
    """Return the value of the named input, if the page has a non-empty one."""
    return _scan(page).hidden.get(name) or None


def method_links(page: str) -> dict[str, str]:
    """Map each offered method id to the link that selects it, in page order."""
    links: dict[str, str] = {}
    for found in _METHOD_LINK.finditer(page):
        link = found.group().replace("&amp;", "&")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        method = query.get("method", [""])[0]
        links.setdefault(method, link)
    links.pop("", None)
    return links


def bank_handover(page: str) -> str | None:
    """Return the address to send the user to for the bank, with its request."""
    request = hidden_value(page, "request")
    if request is None:
        return None
    for target in _scan(page).form_targets:
        if urllib.parse.urlsplit(target).hostname == _BANK_HOST:
            joint = "&" if "?" in target else "?"
            return f"{target}{joint}request={urllib.parse.quote(request, safe='')}"
    return None
