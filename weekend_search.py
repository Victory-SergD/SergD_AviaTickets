#!/usr/bin/env python3
"""
Weekend trip search via Travelpayouts API.

Two modes:

  # 1) Cheapest weekends on a specific route (e.g. SVX <-> AER) over months
  python weekend_search.py route --from SVX --to AER --months 2026-05,2026-06,2026-07,2026-08

  # 2) Discover: cheapest weekend destinations from origin
  python weekend_search.py discover --from SVX --months 2026-05,2026-06,2026-07,2026-08 --top 20

Weekend = depart Friday, return Sunday (2 nights).
Reads TRAVELPAYOUTS_TOKEN from env.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

API_BASE = "https://api.travelpayouts.com"


# Curated international destinations from Russia/CIS — used for "weekend-abroad" hunting.
# Picked for: realistic flight time (<8h), tourism interest, common air-bridge from RU.
INTL_DESTINATIONS: dict[str, str] = {
    # СНГ / соседи
    "ALA": "🇰🇿 Алматы",
    "NQZ": "🇰🇿 Астана",
    "TSE": "🇰🇿 Астана (старый код)",
    "TAS": "🇺🇿 Ташкент",
    "SKD": "🇺🇿 Самарканд",
    "EVN": "🇦🇲 Ереван",
    "GYD": "🇦🇿 Баку",
    "TBS": "🇬🇪 Тбилиси",
    "BUS": "🇬🇪 Батуми",
    "MSQ": "🇧🇾 Минск",
    # Турция
    "IST": "🇹🇷 Стамбул-IST",
    "SAW": "🇹🇷 Стамбул-SAW",
    "AYT": "🇹🇷 Анталия",
    "ESB": "🇹🇷 Анкара",
    "BJV": "🇹🇷 Бодрум",
    "ADB": "🇹🇷 Измир",
    # ОАЭ / Залив
    "DXB": "🇦🇪 Дубай",
    "AUH": "🇦🇪 Абу-Даби",
    "SHJ": "🇦🇪 Шарджа",
    "DOH": "🇶🇦 Доха",
    "RUH": "🇸🇦 Эр-Рияд",
    # Северная Африка / Египет
    "CAI": "🇪🇬 Каир",
    "HRG": "🇪🇬 Хургада",
    "SSH": "🇪🇬 Шарм-эль-Шейх",
    # Азия
    "PEK": "🇨🇳 Пекин",
    "PVG": "🇨🇳 Шанхай",
    "URC": "🇨🇳 Урумчи",
    "HKG": "🇭🇰 Гонконг",
    "BKK": "🇹🇭 Бангкок",
    "HKT": "🇹🇭 Пхукет",
    "KUL": "🇲🇾 Куала-Лумпур",
    "SIN": "🇸🇬 Сингапур",
    "DEL": "🇮🇳 Дели",
    "BOM": "🇮🇳 Мумбаи",
    "MLE": "🇲🇻 Мальдивы",
    # Балканы / Восточная Европа (Шенген/доступ виз)
    "BEG": "🇷🇸 Белград",
    "TIV": "🇲🇪 Тиват",
    "TGD": "🇲🇪 Подгорица",
    "TIA": "🇦🇱 Тирана",
    "SKP": "🇲🇰 Скопье",
    "PRG": "🇨🇿 Прага",
    "BUD": "🇭🇺 Будапешт",
    "VIE": "🇦🇹 Вена",
    "WAW": "🇵🇱 Варшава",
    "LCA": "🇨🇾 Ларнака",
    "ATH": "🇬🇷 Афины",
    "HER": "🇬🇷 Ираклион",
    "RHO": "🇬🇷 Родос",
    "FCO": "🇮🇹 Рим",
    "MXP": "🇮🇹 Милан",
    "BCN": "🇪🇸 Барселона",
    "MAD": "🇪🇸 Мадрид",
    "AMS": "🇳🇱 Амстердам",
    "CDG": "🇫🇷 Париж",
    "FRA": "🇩🇪 Франкфурт",
    "MUC": "🇩🇪 Мюнхен",
    "LHR": "🇬🇧 Лондон",
}


def fmt_duration(minutes: int) -> str:
    h, m = divmod(int(minutes or 0), 60)
    return f"{h}h{m:02d}m"


def weekends_in_months(months: list[str]) -> list[tuple[date, date]]:
    """Yield (Friday, Sunday) pairs whose Friday falls in any of the given YYYY-MM months."""
    today = date.today()
    pairs: list[tuple[date, date]] = []
    for m in months:
        year, month = (int(x) for x in m.split("-"))
        d = date(year, month, 1)
        while d.month == month:
            if d.weekday() == 4 and d >= today:  # Friday
                pairs.append((d, d + timedelta(days=2)))
            d += timedelta(days=1)
    return pairs


@dataclass
class RoundTrip:
    fri: date
    sun: date
    origin: str
    destination: str
    price: float
    currency: str
    airline_out: str
    airline_back: str
    transfers_out: int
    transfers_back: int
    duration_out: int
    duration_back: int
    link: str


def query_prices_for_dates(
    client: httpx.Client,
    token: str,
    origin: str,
    destination: str,
    depart: str,
    return_at: str,
    *,
    direct: bool = False,
    currency: str = "usd",
    limit: int = 5,
) -> list[dict[str, Any]]:
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart,
        "return_at": return_at,
        "one_way": "false",
        "direct": str(direct).lower(),
        "currency": currency,
        "sorting": "price",
        "limit": str(limit),
        "token": token,
    }
    r = client.get(f"{API_BASE}/aviasales/v3/prices_for_dates", params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"{origin}->{destination} {depart}/{return_at}: {r.status_code} {r.text[:200]}")
    payload = r.json()
    return payload.get("data", []) or []


def query_cheap_from_origin(
    client: httpx.Client,
    token: str,
    origin: str,
    depart: str,
    return_at: str,
    *,
    currency: str = "usd",
) -> dict[str, dict[str, Any]]:
    """Discover destinations from origin via /v1/prices/cheap. Returns {DEST: {transfers_str: offer}}."""
    params = {
        "origin": origin,
        "depart_date": depart,
        "return_date": return_at,
        "currency": currency,
        "token": token,
    }
    r = client.get(f"{API_BASE}/v1/prices/cheap", params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"discover {origin} {depart}/{return_at}: {r.status_code} {r.text[:200]}")
    payload = r.json()
    return payload.get("data", {}) or {}


def cheapest_round_trip(
    token: str, origin: str, dest: str, fri: date, sun: date, currency: str
) -> RoundTrip | None:
    """For one weekend, pick cheapest outbound from Friday set + cheapest return from Sunday set."""
    with httpx.Client(timeout=30.0) as client:
        data = query_prices_for_dates(
            client, token, origin, dest,
            fri.isoformat(), sun.isoformat(),
            currency=currency, limit=5,
        )
    if not data:
        return None
    top = min(data, key=lambda d: d["price"])
    return RoundTrip(
        fri=fri, sun=sun,
        origin=top.get("origin", origin),
        destination=top.get("destination", dest),
        price=float(top["price"]),
        currency=currency.upper(),
        airline_out=top.get("airline", "?"),
        airline_back=top.get("return_airline", top.get("airline", "?")),
        transfers_out=int(top.get("transfers", 0)),
        transfers_back=int(top.get("return_transfers", 0)),
        duration_out=int(top.get("duration_to", 0)),
        duration_back=int(top.get("duration_back", 0)),
        link=top.get("link", ""),
    )


def run_route_mode(
    token: str, origin: str, dest: str, months: list[str], currency: str,
) -> list[RoundTrip]:
    weekends = weekends_in_months(months)
    print(f"Checking {len(weekends)} weekends for {origin} <-> {dest}...", file=sys.stderr)
    results: list[RoundTrip] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(cheapest_round_trip, token, origin, dest, fri, sun, currency): (fri, sun)
            for fri, sun in weekends
        }
        for fut in as_completed(futures):
            fri, sun = futures[fut]
            try:
                rt = fut.result()
                if rt:
                    results.append(rt)
            except Exception as e:
                errors.append(f"{fri}: {e}")

    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    results.sort(key=lambda r: r.price)
    return results


AU_DESTINATIONS: dict[str, str] = {
    "SYD": "🇦🇺 Сидней",
    "MEL": "🇦🇺 Мельбурн",
    "BNE": "🇦🇺 Брисбен",
    "PER": "🇦🇺 Перт",
    "ADL": "🇦🇺 Аделаида",
    "OOL": "🇦🇺 Голд-Кост",
    "CNS": "🇦🇺 Кэрнс",
    "DRW": "🇦🇺 Дарвин",
}


def anchor_dates_in_months(months: list[str], weekday: int) -> list[date]:
    """All dates in given YYYY-MM months matching `weekday` (0=Mon, 6=Sun), not in the past."""
    today = date.today()
    out: list[date] = []
    for m in months:
        y, mo = (int(x) for x in m.split("-"))
        d = date(y, mo, 1)
        while d.month == mo:
            if d.weekday() == weekday and d >= today:
                out.append(d)
            d += timedelta(days=1)
    return out


def generate_trip_combos(
    anchors: list[date], nights: int, flex: int,
) -> list[tuple[date, date]]:
    """For each anchor depart-date, expand to (depart, return) pairs with ±flex on each end."""
    pairs: list[tuple[date, date]] = []
    today = date.today()
    seen: set[tuple[date, date]] = set()
    for a in anchors:
        for dd in range(-flex, flex + 1):
            depart = a + timedelta(days=dd)
            if depart < today:
                continue
            base_return = depart + timedelta(days=nights)
            for rd in range(-flex, flex + 1):
                ret = base_return + timedelta(days=rd)
                if ret <= depart:
                    continue
                key = (depart, ret)
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
    return pairs


def run_longtrip_mode(
    token: str, origin: str, dest_codes: list[str], months: list[str],
    *, nights: int, flex: int, depart_dow: int, currency: str, top: int,
) -> list[RoundTrip]:
    # Expand AU alias to full Australian list
    expanded: list[str] = []
    for code in dest_codes:
        if code.upper() == "AU":
            expanded.extend(AU_DESTINATIONS.keys())
        else:
            expanded.append(code.upper())
    expanded = list(dict.fromkeys(expanded))  # dedupe, preserve order
    expanded = [c for c in expanded if c != origin]

    anchors = anchor_dates_in_months(months, depart_dow)
    combos = generate_trip_combos(anchors, nights, flex)
    total = len(expanded) * len(combos)
    print(
        f"Cross-querying {len(combos)} date-combos × {len(expanded)} destinations "
        f"= {total} requests...",
        file=sys.stderr,
    )

    best_per_dest: dict[str, RoundTrip] = {}
    errors: list[str] = []

    def one(dest: str, depart: date, ret: date) -> RoundTrip | None:
        try:
            return cheapest_round_trip(token, origin, dest, depart, ret, currency)
        except Exception as e:
            errors.append(f"{dest} {depart}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [
            ex.submit(one, d, depart, ret)
            for d in expanded
            for (depart, ret) in combos
        ]
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            if completed % 100 == 0:
                print(f"  ...{completed}/{total}", file=sys.stderr)
            rt = fut.result()
            if rt is None:
                continue
            prev = best_per_dest.get(rt.destination)
            if prev is None or rt.price < prev.price:
                best_per_dest[rt.destination] = rt

    if errors and len(errors) < 10:
        for e in errors[:10]:
            print(f"  ! {e}", file=sys.stderr)
    elif errors:
        print(f"  ! {len(errors)} errors suppressed (mostly 'route not flightable')", file=sys.stderr)

    flat = list(best_per_dest.values())
    flat.sort(key=lambda r: r.price)
    return flat[:top]


def print_longtrip_table(results: list[RoundTrip]) -> None:
    if not results:
        print("No long-trip offers found.")
        return
    print(f"\n{'#':<3} {'Code':<5} {'Destination':<22} {'Price':>10} {'Depart':<12} {'Return':<12} {'Nights':>7} {'Stops':>7} Airline")
    print("-" * 115)
    for i, r in enumerate(results, 1):
        nights = (r.sun - r.fri).days
        name = AU_DESTINATIONS.get(r.destination) or INTL_DESTINATIONS.get(r.destination, r.destination)
        stops = f"{r.transfers_out}+{r.transfers_back}"
        print(
            f"{i:<3} {r.destination:<5} {name:<22} {r.price:>7.0f} {r.currency:<2} "
            f"{r.fri.strftime('%a %d-%b'):<12} {r.sun.strftime('%a %d-%b'):<12} "
            f"{nights:>7} {stops:>7}  {r.airline_out}"
        )


def run_summer_abroad_mode(
    token: str, origin: str, months: list[str], currency: str, top: int,
    dest_filter: list[str] | None = None,
) -> list[RoundTrip]:
    """Cross-product: every weekend × every curated international destination.
    Keep cheapest weekend per destination."""
    weekends = weekends_in_months(months)
    destinations = dest_filter or list(INTL_DESTINATIONS.keys())
    destinations = [d for d in destinations if d != origin]
    print(
        f"Cross-querying {len(weekends)} weekends × {len(destinations)} destinations "
        f"= {len(weekends)*len(destinations)} requests...",
        file=sys.stderr,
    )
    best_per_dest: dict[str, RoundTrip] = {}
    errors: list[str] = []

    def one(dest: str, fri: date, sun: date) -> RoundTrip | None:
        try:
            return cheapest_round_trip(token, origin, dest, fri, sun, currency)
        except Exception as e:
            errors.append(f"{dest} {fri}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [
            ex.submit(one, d, fri, sun)
            for d in destinations
            for (fri, sun) in weekends
        ]
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            if completed % 50 == 0:
                print(f"  ...{completed}/{len(futures)}", file=sys.stderr)
            rt = fut.result()
            if rt is None:
                continue
            prev = best_per_dest.get(rt.destination)
            if prev is None or rt.price < prev.price:
                best_per_dest[rt.destination] = rt

    if errors and len(errors) < 10:
        for e in errors[:10]:
            print(f"  ! {e}", file=sys.stderr)
    elif errors:
        print(f"  ! {len(errors)} errors suppressed (mostly 'route not flightable')", file=sys.stderr)

    flat = list(best_per_dest.values())
    flat.sort(key=lambda r: r.price)
    return flat[:top]


def print_abroad_table(results: list[RoundTrip]) -> None:
    if not results:
        print("No international weekend offers found.")
        return
    print(f"\n{'#':<3} {'Code':<5} {'Destination':<28} {'Price':>10} {'Best weekend':<22} {'Stops':>6} Airline")
    print("-" * 110)
    for i, r in enumerate(results, 1):
        weekend = f"{r.fri.strftime('%a %d-%b')} → {r.sun.strftime('%a %d-%b')}"
        stops = f"{r.transfers_out}/{r.transfers_back}"
        name = INTL_DESTINATIONS.get(r.destination, r.destination)
        print(
            f"{i:<3} {r.destination:<5} {name:<28} {r.price:>7.0f} {r.currency:<2} "
            f"{weekend:<22} {stops:>6}  {r.airline_out}"
        )


def run_discover_mode(
    token: str, origin: str, months: list[str], currency: str, top: int,
) -> list[RoundTrip]:
    weekends = weekends_in_months(months)
    print(f"Discovering destinations from {origin} for {len(weekends)} weekends...", file=sys.stderr)

    # For each weekend, query grouped_by_destinations -> get cheapest per dest
    # Then aggregate per dest, keep best weekend per dest
    best_per_dest: dict[str, tuple[RoundTrip, ...]] = {}
    errors: list[str] = []

    def one_weekend(fri: date, sun: date) -> tuple[date, date, dict[str, dict[str, Any]]]:
        with httpx.Client(timeout=30.0) as client:
            return fri, sun, query_cheap_from_origin(
                client, token, origin, fri.isoformat(), sun.isoformat(), currency=currency,
            )

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(one_weekend, fri, sun): (fri, sun) for fri, sun in weekends}
        for fut in as_completed(futures):
            fri, sun = futures[fut]
            try:
                _, _, data = fut.result()
                # Response shape: { DEST: { "0": offer, "1": offer } } where key is transfers count
                for dest_code, by_transfers in data.items():
                    if not isinstance(by_transfers, dict):
                        continue
                    cheapest = min(
                        by_transfers.items(),
                        key=lambda kv: float(kv[1].get("price", float("inf"))),
                    )
                    transfers, offer = cheapest
                    rt = RoundTrip(
                        fri=fri, sun=sun,
                        origin=origin,
                        destination=dest_code,
                        price=float(offer["price"]),
                        currency=currency.upper(),
                        airline_out=offer.get("airline", "?"),
                        airline_back=offer.get("airline", "?"),
                        transfers_out=int(transfers),
                        transfers_back=int(transfers),
                        duration_out=0,
                        duration_back=0,
                        link=offer.get("link", ""),
                    )
                    prev = best_per_dest.get(dest_code)
                    if prev is None or rt.price < prev[0].price:
                        best_per_dest[dest_code] = (rt,)
            except Exception as e:
                errors.append(f"{fri}: {e}")

    for e in errors:
        print(f"  ! {e}", file=sys.stderr)

    flat = [v[0] for v in best_per_dest.values()]
    flat.sort(key=lambda r: r.price)
    return flat[:top]


def print_route_table(results: list[RoundTrip]) -> None:
    if not results:
        print("No weekend offers found.")
        return
    print(f"\n{'#':<3} {'Weekend':<22} {'Price':>10} {'Out':>10}/{'Back':<10} {'Stops':>6}  Airlines")
    print("-" * 100)
    for i, r in enumerate(results, 1):
        weekend = f"{r.fri.strftime('%a %d-%b')} → {r.sun.strftime('%a %d-%b')}"
        out_dur = fmt_duration(r.duration_out) if r.duration_out else "?"
        back_dur = fmt_duration(r.duration_back) if r.duration_back else "?"
        stops = f"{r.transfers_out}/{r.transfers_back}"
        airlines = f"{r.airline_out} / {r.airline_back}"
        print(f"{i:<3} {weekend:<22} {r.price:>7.0f} {r.currency:<2} {out_dur:>10}/{back_dur:<10} {stops:>6}  {airlines}")


def print_discover_table(results: list[RoundTrip]) -> None:
    if not results:
        print("No discovery results.")
        return
    print(f"\n{'#':<3} {'Dest':<6} {'Price':>10} {'Best weekend':<22} {'Out':>10}/{'Back':<10} {'Stops':>6}  Airline")
    print("-" * 105)
    for i, r in enumerate(results, 1):
        weekend = f"{r.fri.strftime('%a %d-%b')} → {r.sun.strftime('%a %d-%b')}"
        out_dur = fmt_duration(r.duration_out) if r.duration_out else "?"
        back_dur = fmt_duration(r.duration_back) if r.duration_back else "?"
        stops = f"{r.transfers_out}/{r.transfers_back}"
        print(f"{i:<3} {r.destination:<6} {r.price:>7.0f} {r.currency:<2} {weekend:<22} {out_dur:>10}/{back_dur:<10} {stops:>6}  {r.airline_out}")


def main() -> int:
    p = argparse.ArgumentParser(description="Weekend trip search via Travelpayouts")
    sub = p.add_subparsers(dest="mode", required=True)

    r = sub.add_parser("route", help="Cheapest weekends on specific route")
    r.add_argument("--from", dest="origin", required=True)
    r.add_argument("--to", dest="dest", required=True)
    r.add_argument("--months", required=True, help="Comma-separated YYYY-MM (e.g. 2026-05,2026-06)")
    r.add_argument("--currency", default="usd")

    d = sub.add_parser("discover", help="Cheapest weekend destinations from origin")
    d.add_argument("--from", dest="origin", required=True)
    d.add_argument("--months", required=True, help="Comma-separated YYYY-MM")
    d.add_argument("--top", type=int, default=20)
    d.add_argument("--currency", default="usd")

    a = sub.add_parser("abroad", help="Cheapest international weekends from origin (curated list)")
    a.add_argument("--from", dest="origin", required=True)
    a.add_argument("--months", required=True, help="Comma-separated YYYY-MM")
    a.add_argument("--top", type=int, default=30)
    a.add_argument("--currency", default="usd")
    a.add_argument("--dests", nargs="+", help="Override destination list (IATA codes)")

    lt = sub.add_parser("longtrip", help="Multi-day trip with flexible dates (e.g. ~1 week with ±1 day flex)")
    lt.add_argument("--from", dest="origin", required=True)
    lt.add_argument("--dests", nargs="+", required=True, help="Destination IATA codes (or 'AU' alias)")
    lt.add_argument("--months", required=True)
    lt.add_argument("--nights", type=int, default=9, help="Approx nights at destination (default 9 = Fri-to-next-Sun)")
    lt.add_argument("--flex", type=int, default=1, help="±N days flex on each end")
    lt.add_argument("--depart-dow", type=int, default=4, help="Departure day-of-week (0=Mon, 4=Fri)")
    lt.add_argument("--top", type=int, default=15)
    lt.add_argument("--currency", default="usd")

    args = p.parse_args()
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("ERROR: set TRAVELPAYOUTS_TOKEN", file=sys.stderr)
        return 2

    months = [m.strip() for m in args.months.split(",")]
    t0 = time.time()

    if args.mode == "route":
        rs = run_route_mode(token, args.origin, args.dest, months, args.currency)
        print(f"Found {len(rs)} weekends in {time.time()-t0:.1f}s", file=sys.stderr)
        print_route_table(rs)
    elif args.mode == "abroad":
        rs = run_summer_abroad_mode(
            token, args.origin, months, args.currency, args.top,
            dest_filter=args.dests,
        )
        print(f"Found {len(rs)} international destinations in {time.time()-t0:.1f}s", file=sys.stderr)
        print_abroad_table(rs)
    elif args.mode == "longtrip":
        rs = run_longtrip_mode(
            token, args.origin, args.dests, months,
            nights=args.nights, flex=args.flex, depart_dow=args.depart_dow,
            currency=args.currency, top=args.top,
        )
        print(f"Found {len(rs)} destinations in {time.time()-t0:.1f}s", file=sys.stderr)
        print_longtrip_table(rs)
    else:
        rs = run_discover_mode(token, args.origin, months, args.currency, args.top)
        print(f"Found {len(rs)} destinations in {time.time()-t0:.1f}s", file=sys.stderr)
        print_discover_table(rs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
