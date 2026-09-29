from bottom.tools import websearch


async def test_fallback_chain(monkeypatch):
    calls = []

    async def tavily(key, q, days, n):
        calls.append("tavily")
        raise RuntimeError("quota")

    async def brave(key, q, days, n):
        calls.append("brave")
        return []

    async def ddg(q, days, n):
        calls.append(("ddg", days))
        return [websearch._row("Binance lists XYZ", "https://binance.com/a", "<b>New</b> listing", "2026-09-25T10:00:00+00:00", "Binance")]

    monkeypatch.setattr(websearch, "_tavily", tavily)
    monkeypatch.setattr(websearch, "_brave", brave)
    monkeypatch.setattr(websearch, "_ddgs", ddg)
    r = await websearch.web_search("XYZ listing", recent_days=2, tavily_key="t", brave_key="b")
    assert calls == ["tavily", "brave", ("ddg", 2)]
    assert r["provider"] == "DuckDuckGo" and r["mode"] == "news"
    assert r["results"][0]["snippet"] == "New listing"
    assert set(r["fallback_after"]) == {"Tavily", "Brave"}


async def test_no_keys_uses_free_search_and_reports_failure(monkeypatch):
    async def ddg(q, days, n):
        raise RuntimeError("blocked")

    monkeypatch.setattr(websearch, "_ddgs", ddg)
    r = await websearch.web_search("anything")
    assert "error" in r and "DuckDuckGo" in r["providers_tried"]


def test_row_and_age():
    row = websearch._row("t", "u", "a&amp;b   c", "2000-01-01T00:00:00Z")
    assert row["snippet"] == "a&b c" and row["age_hours"] > 1000
    assert websearch._age_hours("not a date") is None
    assert websearch._timelimit(1) == "d" and websearch._timelimit(7) == "w" and websearch._timelimit(0) is None


def test_strip_html():
    assert websearch._strip_html("<html><script>x()</script><p>Hello&nbsp;<b>world</b></p></html>") == "Hello world"


async def test_fetch_page_rejects_non_http():
    assert "error" in await websearch.fetch_page("ftp://x")
