"""OI methodology as code: scenario (OI and price direction), long/short combos 1–10, pattern and red flags.

Shared by the own OI card (oi_card) and the parsed Ultra alerts, so both read the market the same way.
Input is a flat dict: oi_chg_pct, price_chg_pct, funding_pct, long_pct, book_delta_usd, oi_mcap_pct, ad_spot_usd,
ad_perp_usd, per_exchange, signal_no.
"""

from __future__ import annotations


SCENARIOS = {
    (True, True): "OI растёт + цена растёт: приходят новые лонгисты — бычий тренд крепкий (лонг или держать)",
    (True, False): "OI растёт + цена падает: приходят новые шортисты — медвежий тренд крепкий (шорт или держать)",
    (False, True): "OI падает + цена растёт: шорты закрываются (шорт-сквиз) — технический отскок, осторожно с лонгом",
    (False, False): "OI падает + цена падает: лонги закрываются/ликвидируются — технический слив, может быть дно, "
                    "осторожно с шортом",
}


def combos(a: dict) -> list[str]:
    """Matches from the OI methodology (combos 1–10). Needs only the fields present in `a`."""
    oi, px = a.get("oi_chg_pct"), a.get("price_chg_pct")
    if oi is None or px is None:
        return []
    up, oi_up = px > 0, oi > 0
    d, lg, f, mc = a.get("book_delta_usd"), a.get("long_pct"), a.get("funding_pct"), a.get("oi_mcap_pct")
    per = a.get("per_exchange") or []
    out = []
    if oi_up and up and d is not None and d > 0:
        out.append("🟢 #1 OI↑ + цена↑ + Delta 📗 — деньги заходят, в стакане давление вверх")
    if oi_up and up and lg is not None and 45 <= lg <= 55:
        out.append("🟢 #2 OI↑ + цена↑ + L/S ~50/50 — тренд только начинается, толпа ещё не зашла")
    if oi_up and up and mc is not None and mc > 5:
        out.append("🟢 #3 OI↑ + цена↑ + OI/MCap >5% — серьёзное движение для монеты")
    if not oi_up and up and lg is not None and lg < 40:
        out.append("🟢 #4 OI↓ + цена↑ + L/S <40% — шорт-сквиз в действии, цену выносит вверх")
    if oi_up and up and len(per) >= 2 and all((x.get("book_delta_usd") or 0) > 0 for x in per):
        out.append("🟢 #5 Delta 📗 на обеих биржах + цена↑ — самый сильный бычий сигнал")
    if oi_up and not up and d is not None and d < 0 and lg is not None and lg > 60:
        out.append("🔴 #6 OI↑ + цена↓ + Delta 📕 + L/S >60% — идеальный шорт: лонги в ловушке")
    if oi_up and not up and f is not None and f >= 0.5 and lg is not None and lg > 65:
        out.append("🔴 #7 OI↑ + цена↓ + фандинг +0.5%+ + L/S >65% — перегруженные лонги платят, скоро ликвидации")
    if oi_up and not up and mc is not None and mc > 10:
        out.append("🔴 #8 OI↑ + цена↓ + OI/MCap >10% — сильное давление шортов")
    if oi_up and up and d is not None and d < 0:
        out.append("🔴 #9 OI↑ + цена↑ + Delta 📕 — расхождение: толпа покупает, а в стакане продают в лонгистов")
    if oi_up and up and lg is not None and lg > 75:
        out.append("🔴 #10 OI↑ + цена↑ + L/S >75% — толпа экстремально в лонге, скоро ликвидации сверху")
    return out


def classify(a: dict) -> dict:
    """Playbook pattern for one alert (or the latest state of a series) + methodology combos and scenario."""
    out = _pattern(a)
    oi, px = a.get("oi_chg_pct"), a.get("price_chg_pct")
    if oi is not None and px is not None and oi != 0:
        out["scenario"] = SCENARIOS[(oi > 0, px > 0)]
    out["combos"] = combos(a)
    return out


def _pattern(a: dict) -> dict:
    oi = a.get("oi_chg_pct", 0)
    px = a.get("price_chg_pct")
    f = a.get("funding_pct")
    lg = a.get("long_pct")
    flags = []
    pattern = "нет данных"
    if oi < 0 and px is not None:
        pattern = ("OI падает на росте — закрытие шортов, технический отскок" if px > 0
                   else "OI падает на падении — лонги выходят/ликвидации, возможно дно")
    elif px is not None and f is not None and lg is not None:
        if abs(px) < 2 and abs(f) < 0.02 and 45 <= lg <= 55:
            pattern = "шум: ротация позиций без направления"
        elif oi > 0 and px > 0 and f < 0 and lg < 50:
            pattern = "шорт-сквиз: шорты набираются против роста и платят — продолжение вероятно"
        elif oi > 0 and px > 0 and f < 0:
            pattern = "рост при отрицательном фандинге — шорты платят, возможен сквиз"
        elif oi > 0 and px < 0 and lg >= 65:
            pattern = "ловушка лонгистов: лонги набирают на падении — риск каскада ликвидаций вниз"
        elif oi > 0 and px < 0 and lg >= 58:
            pattern = "лонги усредняются на падении — толпа ловит нож, риск ловушки лонгистов"
        elif oi > 0 and px < 0 and lg < 50:
            pattern = "шорты набираются на падении — тренд вниз, но топливо для сквиза копится"
        elif oi > 0 and px > 0 and f >= 0.05 and lg >= 65:
            pattern = "перегрев лонгов: толпа в лонге и платит — риск ликвидаций сверху"
        elif oi > 0 and px > 0 and lg <= 55:
            pattern = "чистый лонг-моментум: новые деньги без перегрузки толпы"
        elif oi > 0 and px > 0:
            pattern = "лонг-моментум, толпа уже в лонге — вход только на откате"
        else:
            pattern = "неоднозначно"
    elif px is None and lg is not None and oi > 0:
        # some alerts carry the price without its change — judge by crowd and funding, direction from candles
        if lg <= 35:
            pattern = "шорты набирают позиции против рынка — топливо для сквиза, триггер нужен рост цены"
        elif lg >= 65:
            pattern = "лонги перегружены — риск слива; направление цены в алерте нет, смотри свечи"
        else:
            pattern = "в алерте нет изменения цены — направление смотри по свечам"
    if px is not None and abs(px) < 2 and oi >= 20:
        flags.append("OI растёт при стоящей цене — набор позиций перед движением")
    mc = a.get("oi_mcap_pct")
    if mc is not None:
        if mc > 1000:
            flags.append(f"OI/MCap {mc}% — спекулятивный пузырь, не торговать")
        elif mc > 300:
            flags.append(f"OI/MCap {mc}% — только скальп")
        elif mc > 100:
            flags.append(f"OI/MCap {mc}% — повышенная перегрузка деривативами")
    sp, pp = a.get("ad_spot_usd"), a.get("ad_perp_usd")
    if sp is not None and sp > 0 and (pp or 0) > 0:
        flags.append("спот и перп берут — органический спрос")
    elif sp is not None and sp < 0 < oi:
        flags.append("спот продают при растущем OI — рост держится на плечах")
    if pp is not None and pp < 0 < oi:
        flags.append("перп A/D отрицательный при растущем OI — признак дистрибуции")
    if f is not None and f >= 0.1:
        flags.append(f"фандинг +{f}% — перегрев (порог +0.1% уже сигнал)")
    if f is not None and f <= -0.5:
        flags.append(f"фандинг {f}% — шорты платят очень много")
    if (a.get("oi_fast_chg_pct") or 0) >= 10:
        flags.append(f"быстрый набор OI: +{a['oi_fast_chg_pct']}% за {a.get('oi_fast_window_min')} мин")
    if px is not None and abs(px) >= 15:
        flags.append(f"цена уже {px:+}% — движение во многом прошло до алерта, вход в погоню опасен")
    return {"pattern": pattern, "flags": flags}
