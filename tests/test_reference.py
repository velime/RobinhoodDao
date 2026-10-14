import json
import sqlite3

from bottom import reference as ref
from bottom.tools.setup_calc import entry_status, stop_hunt_warnings

LIMIT_CALL = """📣 Maga AI call — $CAP

💡 Идея
Шорт-сквиз: фандинг отрицательный, шорты платят.

🎯 Сетап

Лонг перп (Binance/Bybit).
Вход лимит: $0.032591 (bid-стенка)
Стоп-заявка: $0.030900 (–5.2% от входа, за стенкой $0.031079)
Слом идеи: закрытие 15м ниже $0.031079
Цель 1: $0.036418 · R:R ≈ 2.3:1
Цель 2: $0.038190 · R:R ≈ 3.3:1
⚠ Агрессивный от ~$0.033200: стоп за стенку не влезает в 6% — без чистого входа"""

RANGES_CALL = """📣 Maga AI call — GRAM

💡 Идея
Флип OI: OI↑ + цена↑ + book delta↑.

🎯 Сетап
Лонг перпа GRAMUSDT (Binance / Bybit)
Вход от текущих: $1.52–1.53 (агрессивно, нарратив в ходу)
Вход по откату: $1.50–1.51 (стенка поддержки $271K — для тех, кто ждёт)
Цели: $1.64 → $1.80
Стоп: $1.45"""

SHORT_CALL = """📣 Maga AI call — $NMR

💡 Идея
Отработанный сквиз: вертикаль +26% за 4ч, приоритет — откат.

🎯 Сетап

Шорт перп (Binance/Bybit)
Основной: лимит $11.35–$11.40 (отскок → R:R ~2.1:1)
Агрессивный: $11.25–$11.28 (от текущих → R:R ~1.3:1, повышенный риск)
Слом идеи: закрытие 1ч выше $11.75
Стоп-заявка: $11.82
TP1: $10.50 (перед 1ч пулом ликвидности $10.445)
TP2: $10.20
После TP1 → стоп в безубыток"""


def test_parse_setup_formats():
    a = ref.parse_setup(LIMIT_CALL)
    assert a["direction"] == "long" and (a["entry_low"], a["entry_high"]) == (0.032591, 0.032591)
    assert a["stop"] == 0.0309 and a["targets"][:2] == [0.036418, 0.03819] and a["limit"]
    b = ref.parse_setup(RANGES_CALL)
    assert (b["entry_low"], b["entry_high"]) == (1.5, 1.51) and b["aggressive"] == [1.52, 1.53]
    assert b["stop"] == 1.45 and b["targets"] == [1.64, 1.8]
    c = ref.parse_setup(SHORT_CALL)
    assert c["direction"] == "short" and (c["entry_low"], c["entry_high"]) == (11.35, 11.4)
    assert c["stop"] == 11.82 and c["targets"] == [10.5, 10.2] and c["aggressive"] == [11.25, 11.28]
    assert ref.parse_setup(SHORT_CALL.replace("$11.82", "$9.00")) is None  # stop on the wrong side
    assert ref.tags_of(SHORT_CALL) == ["exhausted_squeeze"] and "oi_flip" in ref.tags_of(RANGES_CALL)


def _export(tmp_path):
    msgs = [
        {"id": 1, "type": "message", "date": "2026-08-06T06:04:00", "from": "Maga caller", "text": LIMIT_CALL},
        {"id": 2, "type": "message", "date": "2026-08-06T07:00:00", "from": "Chili", "reply_to_message_id": 1,
         "text": "12% чистого, первый тейк"},
        {"id": 3, "type": "message", "date": "2026-08-06T07:05:00", "from": "werollin", "reply_to_message_id": 2,
         "text": [{"type": "bold", "text": "уже 18% чистого"}]},
        {"id": 4, "type": "message", "date": "2026-08-07T10:00:00", "from": "Maga caller",
         "text": "👀 Maga AI watch — FLOCK\n\nЖду откат к зоне — пока без сделки"},
        {"id": 5, "type": "message", "date": "2026-08-07T11:00:00", "from": "Maga caller",
         "text": "💨 Maga AI — вход ушёл — CAP\n\nцена ушла"},
        {"id": 6, "type": "message", "date": "2026-08-08T11:00:00", "from": "Maga caller", "text": SHORT_CALL},
        {"id": 7, "type": "message", "date": "2026-08-08T12:00:00", "from": "Chili", "reply_to_message_id": 6,
         "text": "Стоп 🌟"},
    ]
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"name": "Maga AI calls", "messages": msgs}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_parse_export_and_reported(tmp_path):
    recs, st = ref.parse_export(_export(tmp_path))
    assert st == {"call": 2, "call_parsed": 2, "watch": 1, "entry_gone": 1}
    cap = next(r for r in recs if r["msg_id"] == 1)
    assert cap["reported"] == [12.0, 18.0] and cap["ticker"] == "CAP"
    nmr = next(r for r in recs if r["msg_id"] == 6)
    assert nmr["reported_stop"] and nmr["ticker"] == "NMR"


def _candles(start_s, path):
    return [{"t": int((start_s + i * 3600) * 1000), "o": o, "h": h, "l": lo, "c": c, "v": 1.0}
            for i, (o, h, lo, c) in enumerate(path)]


async def test_store_evaluate_stats_lookup(tmp_path, monkeypatch):
    db = sqlite3.connect(str(tmp_path / "j.db"))
    recs, _ = ref.parse_export(_export(tmp_path))
    assert ref.store(db, recs, "maga") == 4 and ref.store(db, recs, "maga") == 0  # idempotent
    calls = {c["symbol"]: c for c in ref.open_calls(db)}
    assert set(calls) == {"CAP", "NMR"}

    async def fake_candles(pool, symbol, since_s, hours):
        if symbol == "CAP":  # dips into the limit, then runs through both targets
            return _candles(since_s, [(0.0335, 0.0336, 0.0325, 0.0330), (0.033, 0.0366, 0.0329, 0.0365),
                                      (0.0365, 0.0385, 0.036, 0.038)])
        return _candles(since_s, [(11.3, 11.45, 11.2, 11.4), (11.4, 11.9, 11.3, 11.85)])  # filled, then stopped

    monkeypatch.setattr(ref, "candles_since", fake_candles)
    assert await ref.evaluate(db, object()) == 2
    out = dict(db.execute("SELECT symbol, outcome FROM reference_calls WHERE status='closed'").fetchall())
    assert out == {"CAP": "tp2", "NMR": "stop"}
    st = ref.stats(db, min_n=1)
    assert st["все"]["filled"] == 2 and st["все"]["win_rate"] == 50 and st["long"]["win_rate"] == 100
    assert st["exhausted_squeeze short"]["avg_r"] == -1.0
    lines = ref.experience_lines(db)
    assert lines == [] or lines[0].startswith("Референс")  # min 5 trades per group in the prompt
    look = ref.lookup(db, symbol="$cap")
    assert look["calls"][0]["outcome"] == "tp2" and look["calls"][0]["reported"]["pct"] == [12.0, 18.0]


def test_bundled_reference_loads(tmp_path):
    db = sqlite3.connect(str(tmp_path / "b.db"))
    n = ref.load_bundled(db)
    assert n > 500 and ref.load_bundled(db) == 0
    parsed = db.execute("SELECT COUNT(*) FROM reference_calls WHERE stop IS NOT NULL").fetchone()[0]
    assert parsed > 200


def test_entry_status_and_stop_hunt():
    assert entry_status("long", 1.0, 1.02, 0.95, 1.10, 1.01)["status"] == "in_zone"
    assert entry_status("long", 1.0, 1.02, 0.95, 1.10, 1.04)["status"] == "away"
    gone = entry_status("long", 1.0, 1.02, 0.95, 1.10, 1.07)
    assert gone["status"] == "gone" and gone["progress_to_tp1_pct"] == 62.5
    assert entry_status("long", 1.0, 1.02, 0.95, 1.10, 0.94)["status"] == "invalid"
    assert entry_status("short", 1.0, 1.02, 1.08, 0.90, 0.96)["status"] == "away"
    assert entry_status("short", 1.0, 1.02, 1.08, 0.90, 1.05)["status"] == "past_zone"
    w = stop_hunt_warnings("long", 0.308, 0.308, 0.2984, [(0.2848, "лоу 4ч"), (0.25, "свинг 4ч"), (0.31, "свинг 1ч")])
    assert len(w) == 1 and w[0].startswith("лоу 4ч 0.2848") and "1.42 риска" in w[0]
    assert stop_hunt_warnings("short", 11.4, 11.4, 11.82, [(11.9, "хай 24ч")])[0].startswith("хай 24ч 11.9")


async def test_calc_setup_wrapper_adds_status(monkeypatch):
    from bottom.config import Settings
    from bottom.tools.registry import Toolbox

    tb = Toolbox(Settings())

    async def fake_candles(symbol, tf, limit=200):
        if tf == "1h":
            return {"atr14_pct": 0.8, "last_close": 1.07, "levels": {"support": [0.94, 0.9], "resistance": [1.2]},
                    "range_24h": {"high": 1.12, "low": 0.97}}
        return {"levels": {"support": [0.93], "resistance": []}}

    monkeypatch.setattr(tb, "_candles", fake_candles)
    try:
        r = await tb._calc_setup("ABC", "long", 1.0, 0.95, [1.10, 1.2], 1.02)
        assert r["entry_status"]["status"] == "gone"
        assert any("сразу за стопом" in w for w in r.get("warnings", []))
        r = await tb._calc_setup("ABC", "long", 1.10, 1.08, [1.3], 1.12)
        assert r["verdict"] == "REJECT" and any("за стопом" in p for p in r["problems"])
    finally:
        await tb.close()
