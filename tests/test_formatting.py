from swarm.formatting import strip_markup, to_html_blocks, to_telegram_chunks


def test_escape_and_bold():
    blocks = to_html_blocks("Цена <$1> & **рост** `BTCUSDT`")
    assert blocks == ["Цена &lt;$1&gt; &amp; <b>рост</b> <code>BTCUSDT</code>"]


def test_quotes():
    text = "Вывод\n\n>! факт 1\n>! факт 2\n\n> обычная цитата\nтекст"
    blocks = to_html_blocks(text)
    assert blocks[0] == "Вывод"
    assert blocks[1] == "<blockquote expandable>факт 1\nфакт 2</blockquote>"
    assert blocks[2] == "<blockquote>обычная цитата</blockquote>"
    assert blocks[3] == "текст"


def test_bullets_and_headers():
    blocks = to_html_blocks("## Итог\n- один\n* два")
    assert blocks == ["<b>Итог</b>\n• один\n• два"]


def test_chunking():
    text = "\n\n".join(f"абзац {i} " + "x" * 500 for i in range(20))
    chunks = to_telegram_chunks(text, limit=2000)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks).count("абзац") == 20


def test_strip_markup():
    assert strip_markup("**a**\n>! b") == "a\nb"
