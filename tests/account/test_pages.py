"""Tests for reading Posti's login pages."""

from __future__ import annotations

import urllib.parse

from custom_components.posti.account import pages

SELECT = "/uas/authn/abc/select?method=telia.ftn.oidc.op.1"
LOGIN_PAGE = (
    f"<a href='{SELECT}'>OP</a>"
    '<a href="/uas/authn/abc/select?method=telia.ftn.oidc.mobileid.1">Mobile</a>'
    "<a href='/uas/authn/abc/select?method=telia.ftn.oidc.op.1'>dup</a>"
    "<a href='/other'>x</a>"
)
BANK_PAGE = (
    "<form method='post' action='https://tunnistus.telia.fi/idp/auth?a=1&amp;b=2'>"
    "<input type='hidden' name='request' value='sig+nature=='/></form>"
)


def test_methods_are_read_in_page_order_without_duplicates():
    methods = pages.method_links(LOGIN_PAGE)
    assert list(methods) == ["telia.ftn.oidc.op.1", "telia.ftn.oidc.mobileid.1"]
    assert methods["telia.ftn.oidc.op.1"] == SELECT


def test_methods_are_found_inside_script_text_and_unescaped():
    page = '<script>go("/uas/authn/x/select?method=m.1&amp;lang=fi")</script>'
    assert pages.method_links(page) == {"m.1": "/uas/authn/x/select?method=m.1&lang=fi"}


def test_no_methods_on_an_unrelated_page():
    assert pages.method_links("<html>nothing</html>") == {}


def test_bank_address_carries_the_signed_request():
    assert pages.bank_handover(BANK_PAGE) == (
        "https://tunnistus.telia.fi/idp/auth?a=1&b=2"
        f"&request={urllib.parse.quote('sig+nature==', safe='')}"
    )


def test_bank_address_without_a_query_gets_one():
    page = (
        "<form action='https://tunnistus.telia.fi/idp/auth'>"
        "<input name='request' value='r'></form>"
    )
    assert pages.bank_handover(page) == "https://tunnistus.telia.fi/idp/auth?request=r"


def test_bank_address_fails_closed():
    assert pages.bank_handover("<form action='https://elsewhere.example/'>") is None
    assert pages.bank_handover("<html></html>") is None
    other = "<form action='https://elsewhere.example/'><input name='request' value='r'>"
    assert pages.bank_handover(other) is None


def test_hidden_value_ignores_empty_and_missing_inputs():
    page = "<input name='a' value=''><input name='b' value='x&amp;y'>"
    assert pages.hidden_value(page, "a") is None
    assert pages.hidden_value(page, "b") == "x&y"
    assert pages.hidden_value(page, "c") is None
