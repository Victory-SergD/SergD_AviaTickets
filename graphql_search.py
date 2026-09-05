#!/usr/bin/env python3
"""
Itinerary search via Aviasales GraphQL Flights Data API (Travelpayouts).

Why a separate script: the REST endpoints (/aviasales/v3/prices_for_dates etc.) return ONE cheapest
offer per date and no flight numbers. GraphQL returns dozens of offers per date with full segments:
every flight leg (carrier, flight number, departure/arrival with timezone), every transfer
(airport, country, duration, night/visa flags), gate and a deep link. Same token, no extra access.

Two modes:

  # 1) All one-way itineraries A->B on given dates, sorted by price or duration
  python graphql_search.py search --from SVX --to CGK --dates 2026-09-08,2026-09-09,2026-09-10

  # 2) Stopover hunter: A->HUB + HUB->B as two tickets, with a REAL feasibility check
  #    (leg 2 departs after leg 1 arrives + connection buffer, stop within [min, max] hours)
  python graphql_search.py stopover --from SVX --to CGK --dates 2026-09-08,2026-09-09,2026-09-10 \
      --min-stop 16 --max-stop 50 --max-price 850 --exclude-countries CN TH MY SG VN TR UZ

  # same, but as a fast transit (2.5..8h connection) — finds self-transfer routes REST never shows
  python graphql_search.py stopover --from SVX --to CGK --dates 2026-09-10 --min-stop 2.5 --max-stop 8

Reads TRAVELPAYOUTS_TOKEN from env. Prices are Aviasales cache (found within ~48h), not a live search.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

GRAPHQL_URL = "https://api.travelpayouts.com/graphql/v1/query"
AVIASALES_SEARCH = "https://www.aviasales.ru/search"
MAX_DEPART_DATES = 5      # API limit: "length of depart_dates exceeds allowable maximum of 5"
RETRY_BACKOFF_S = (2, 4, 8, 16, 32)  # on HTTP 429 / 5xx; the GraphQL rate limit is tight, keep workers low

# Curated stopover hubs: IATA -> (name, ISO country). Country is used for --exclude-countries.
STOPOVER_HUBS: dict[str, tuple[str, str]] = {
    # Gulf
    "DXB": ("Дубай", "AE"),
    "DWC": ("Дубай Аль-Мактум", "AE"),
    "AUH": ("Абу-Даби", "AE"),
    "SHJ": ("Шарджа", "AE"),
    "DOH": ("Доха", "QA"),
    "MCT": ("Маскат", "OM"),
    "BAH": ("Манама", "BH"),
    # CIS / Caucasus
    "ALA": ("Алматы", "KZ"),
    "NQZ": ("Астана", "KZ"),
    "TAS": ("Ташкент", "UZ"),
    "EVN": ("Ереван", "AM"),
    "TBS": ("Тбилиси", "GE"),
    "GYD": ("Баку", "AZ"),
    "MOW": ("Москва", "RU"),
    # Turkey / Egypt
    "IST": ("Стамбул", "TR"),
    "CAI": ("Каир", "EG"),
    # South Asia
    "DEL": ("Дели", "IN"),
    "BOM": ("Мумбаи", "IN"),
    "CMB": ("Коломбо", "LK"),
    "MLE": ("Мале", "MV"),
    # East / South-East Asia
    "PEK": ("Пекин", "CN"),
    "PVG": ("Шанхай", "CN"),
    "CAN": ("Гуанчжоу", "CN"),
    "URC": ("Урумчи", "CN"),
    "HKG": ("Гонконг", "HK"),
    "ICN": ("Сеул", "KR"),
    "NRT": ("Токио", "JP"),
    "TPE": ("Тайбэй", "TW"),
    "MNL": ("Манила", "PH"),
    "BKK": ("Бангкок", "TH"),
    "KUL": ("Куала-Лумпур", "MY"),
    "SIN": ("Сингапур", "SG"),
    "SGN": ("Хошимин", "VN"),
    "HAN": ("Ханой", "VN"),
}

# Fields we ask GraphQL for. `segments` carries the full itinerary.
PRICE_FIELDS = """
  departure_at value currency duration number_of_changes gate main_airline with_baggage ticket_link
  origin_airport_iata destination_airport_iata
  segments {
    departure_at arrival_at
    transfers { at to country_code duration_seconds night_transfer visa_required }
    flight_legs { origin destination departure_at arrival_at flight_number operating_carrier aircraft_code }
  }
"""


def fmt_hours(hours: float) -> str:
    h = int(hours)
    m = int(round((hours - h) * 60))
    return f"{h}h{m:02d}m"


def parse_dt(s: str) -> datetime:
    """Aviasales returns local time with offset, e.g. 2026-09-10T15:20:00+05:00."""
    return datetime.fromisoformat(s)


@dataclass
class Leg:
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    carrier: str
    flight_number: str


@dataclass
class Transfer:
    at: str
    to: str
    country_code: str
    duration_min: int
    night: bool
    visa_required: bool


@dataclass
class Itinerary:
    price: float
    currency: str
    legs: list[Leg]
    transfers: list[Transfer]
    gate: str
    ticket_link: str
    duration_min: int  # flight time only, as reported by API

    @property
    def departure_at(self) -> datetime:
        return self.legs[0].departure_at

    @property
    def arrival_at(self) -> datetime:
        return self.legs[-1].arrival_at

    @property
    def elapsed_hours(self) -> float:
        """Door-to-door, timezone-aware (arrival - departure)."""
        return (self.arrival_at - self.departure_at).total_seconds() / 3600

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    @property
    def route(self) -> str:
        return " > ".join([self.legs[0].origin] + [l.destination for l in self.legs])

    @property
    def flights(self) -> str:
        return "/".join(f"{l.carrier}{l.flight_number}" for l in self.legs)

    @property
    def link(self) -> str:
        return f"{AVIASALES_SEARCH}{self.ticket_link}" if self.ticket_link else ""

    @property
    def key(self) -> str:
        """Identity of the physical itinerary (flights + times), regardless of price/gate."""
        return "|".join(f"{l.carrier}{l.flight_number}@{l.departure_at.isoformat()}" for l in self.legs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "currency": self.currency,
            "departure_at": self.departure_at.isoformat(),
            "arrival_at": self.arrival_at.isoformat(),
            "elapsed_hours": round(self.elapsed_hours, 2),
            "stops": self.stops,
            "route": self.route,
            "flights": self.flights,
            "gate": self.gate,
            "link": self.link,
            "legs": [
                {
                    "origin": l.origin, "destination": l.destination,
                    "departure_at": l.departure_at.isoformat(), "arrival_at": l.arrival_at.isoformat(),
                    "flight": f"{l.carrier}{l.flight_number}",
                }
                for l in self.legs
            ],
            "transfers": [
                {"at": t.at, "country": t.country_code, "duration_min": t.duration_min,
                 "night": t.night, "visa_required": t.visa_required}
                for t in self.transfers
            ],
        }


def parse_itinerary(raw: dict[str, Any]) -> Itinerary | None:
    legs: list[Leg] = []
    transfers: list[Transfer] = []
    for seg in raw.get("segments") or []:
        for fl in seg.get("flight_legs") or []:
            legs.append(
                Leg(
                    origin=fl["origin"],
                    destination=fl["destination"],
                    departure_at=parse_dt(fl["departure_at"]),
                    arrival_at=parse_dt(fl["arrival_at"]),
                    carrier=fl.get("operating_carrier", "?"),
                    flight_number=str(fl.get("flight_number", "")),
                )
            )
        for tr in seg.get("transfers") or []:
            transfers.append(
                Transfer(
                    at=tr["at"], to=tr["to"], country_code=tr.get("country_code", ""),
                    duration_min=int(tr.get("duration_seconds", 0)) // 60,
                    night=bool(tr.get("night_transfer")), visa_required=bool(tr.get("visa_required")),
                )
            )
    if not legs:
        return None
    return Itinerary(
        price=float(raw["value"]),
        currency=str(raw.get("currency", "")).upper(),
        legs=legs,
        transfers=transfers,
        gate=raw.get("gate", "") or "",
        ticket_link=raw.get("ticket_link", "") or "",
        duration_min=int(raw.get("duration") or 0),
    )


def gql(client: httpx.Client, token: str, query: str) -> dict[str, Any]:
    """POST a GraphQL query; retry with backoff on 429 (rate limit) and 5xx."""
    for attempt, pause in enumerate((*RETRY_BACKOFF_S, None)):
        r = client.post(GRAPHQL_URL, json={"query": query}, headers={"X-Access-Token": token})
        if r.status_code == 429 or r.status_code >= 500:
            if pause is None:
                raise RuntimeError(f"GraphQL HTTP {r.status_code} after {attempt} retries: {r.text[:120]}")
            time.sleep(pause)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"GraphQL HTTP {r.status_code}: {r.text[:200]}")
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL error: {json.dumps(payload['errors'])[:300]}")
        return payload["data"]
    raise AssertionError("unreachable")


def query_one_way(
    token: str,
    origin: str,
    destination: str,
    dates: list[str] | None = None,
    *,
    date_range: tuple[str, str] | None = None,
    direct: bool | None = None,
    currency: str = "usd",
    market: str = "ru",
    limit: int = 300,
) -> list[Itinerary]:
    """All cached one-way offers origin->destination.

    Pass either `dates` (explicit YYYY-MM-DD list; the API takes max 5 per call, so we chunk)
    or `date_range=(min, max)` (inclusive). Duplicates of the same ticket via several gates are dropped.
    """
    direct_gql = "" if direct is None else f", direct: {'true' if direct else 'false'}"
    param_sets: list[str] = []
    if date_range:
        param_sets.append(f'depart_date_min: "{date_range[0]}", depart_date_max: "{date_range[1]}"')
    for i in range(0, len(dates or []), MAX_DEPART_DATES):
        chunk = ",".join(f'"{d}"' for d in (dates or [])[i : i + MAX_DEPART_DATES])
        param_sets.append(f"depart_dates: [{chunk}]")
    if not param_sets:
        raise ValueError("query_one_way: need dates or date_range")

    best: dict[str, Itinerary] = {}  # same flights+times sold by several gates -> keep the cheapest
    with httpx.Client(timeout=90.0) as client:
        for params in param_sets:
            query = f"""
            {{ prices_one_way(
                params: {{ origin: "{origin}", destination: "{destination}", {params}{direct_gql} }},
                paging: {{ limit: {limit}, offset: 0 }}, sorting: VALUE_ASC, grouping: NONE,
                currency: "{currency}", market: "{market}"
              ) {{ {PRICE_FIELDS} }} }}
            """
            data = gql(client, token, query)
            for raw in data.get("prices_one_way") or []:
                it = parse_itinerary(raw)
                if it is None:
                    continue
                key = it.key
                if key not in best or it.price < best[key].price:
                    best[key] = it
    return list(best.values())


# ----------------------------------------------------------------------------- search mode


def run_search(
    token: str, origin: str, dest: str, dates: list[str], *,
    direct: bool | None, max_hours: float | None, max_price: float | None,
    sort: str, currency: str, market: str,
) -> list[Itinerary]:
    its = query_one_way(token, origin, dest, dates, direct=direct, currency=currency, market=market)
    if max_hours is not None:
        its = [i for i in its if i.elapsed_hours <= max_hours]
    if max_price is not None:
        its = [i for i in its if i.price <= max_price]
    if sort == "duration":
        its.sort(key=lambda i: (i.elapsed_hours, i.price))
    else:
        its.sort(key=lambda i: (i.price, i.elapsed_hours))
    return its


def print_itineraries(its: list[Itinerary], top: int, *, show_links: bool) -> None:
    if not its:
        print("No offers found.")
        return
    print(f"\n{'#':<3} {'Price':>9} {'Depart':<12} {'Arrive':<12} {'Total':>8} {'Stops':>5}  {'Flights':<30} Route  [gate]")
    print("-" * 130)
    for i, it in enumerate(its[:top], 1):
        print(
            f"{i:<3} {it.price:>6.0f} {it.currency:<2} "
            f"{it.departure_at.strftime('%d.%m %H:%M'):<12} {it.arrival_at.strftime('%d.%m %H:%M'):<12} "
            f"{fmt_hours(it.elapsed_hours):>8} {it.stops:>5}  {it.flights[:30]:<30} {it.route}  [{it.gate}]"
        )
        for t in it.transfers:
            flags = " ночь" if t.night else ""
            flags += " ВИЗА" if t.visa_required else ""
            print(f"      ↳ пересадка {t.at} ({t.country_code}) {fmt_hours(t.duration_min / 60)}{flags}")
        if show_links and it.link:
            print(f"      {it.link}")


# ----------------------------------------------------------------------------- stopover mode


@dataclass
class Combo:
    hub: str
    hub_name: str
    country: str
    leg1: Itinerary
    leg2: Itinerary
    stop_hours: float

    @property
    def price(self) -> float:
        return self.leg1.price + self.leg2.price

    @property
    def elapsed_hours(self) -> float:
        return (self.leg2.arrival_at - self.leg1.departure_at).total_seconds() / 3600

    @property
    def airport_change(self) -> bool:
        return self.leg1.legs[-1].destination != self.leg2.legs[0].origin

    def to_dict(self) -> dict[str, Any]:
        return {
            "hub": self.hub, "hub_name": self.hub_name, "country": self.country,
            "price": round(self.price, 2), "elapsed_hours": round(self.elapsed_hours, 2),
            "stop_hours": round(self.stop_hours, 2), "airport_change": self.airport_change,
            "leg1": self.leg1.to_dict(), "leg2": self.leg2.to_dict(),
        }


def leg2_date_range(dates: list[str], max_stop_hours: float) -> tuple[str, str]:
    """Leg 2 may depart the day leg 1 leaves (fast transit) or up to max_stop later — one date range covers it."""
    span_days = int(max_stop_hours // 24) + 3  # +2 for long leg-1 itineraries, +1 rounding
    first = min(datetime.strptime(d, "%Y-%m-%d") for d in dates)
    last = max(datetime.strptime(d, "%Y-%m-%d") for d in dates) + timedelta(days=span_days)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def run_stopover(
    token: str, origin: str, dest: str, dates: list[str], hubs: list[str], *,
    min_stop: float, max_stop: float, min_connect_airport_change: float,
    max_price: float | None, max_hours: float | None, exclude_countries: set[str], sort: str,
    currency: str, market: str, workers: int = 1,
) -> list[Combo]:
    dates2 = leg2_date_range(dates, max_stop)
    errors: list[str] = []

    def fetch(hub: str) -> tuple[str, list[Itinerary], list[Itinerary]]:
        l1 = query_one_way(token, origin, hub, dates, currency=currency, market=market)
        l2 = query_one_way(token, hub, dest, date_range=dates2, currency=currency, market=market) if l1 else []
        return hub, l1, l2

    hubs = [h for h in hubs if h not in (origin, dest)]
    print(f"Searching {origin}->{dest} via {len(hubs)} hubs, stop {min_stop}-{max_stop}h ...", file=sys.stderr)
    combos: list[Combo] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, h): h for h in hubs}
        for fut in as_completed(futures):
            hub = futures[fut]
            try:
                _, leg1s, leg2s = fut.result()
            except Exception as e:
                errors.append(f"{hub}: {e}")
                continue
            name, country = STOPOVER_HUBS.get(hub, (hub, "??"))
            if country in exclude_countries:
                continue
            for a in leg1s:
                for b in leg2s:
                    gap = (b.departure_at - a.arrival_at).total_seconds() / 3600
                    if gap < min_stop or gap > max_stop:
                        continue
                    if a.legs[-1].destination != b.legs[0].origin and gap < min_connect_airport_change:
                        continue
                    if max_price is not None and a.price + b.price > max_price:
                        continue
                    combo = Combo(hub, name, country, a, b, gap)
                    if max_hours is not None and combo.elapsed_hours > max_hours:
                        continue
                    combos.append(combo)
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)

    # Same physical flights from different gates — keep the cheapest.
    uniq: dict[str, Combo] = {}
    for c in combos:
        key = f"{c.leg1.key}||{c.leg2.key}"
        if key not in uniq or c.price < uniq[key].price:
            uniq[key] = c
    combos = list(uniq.values())
    if sort == "duration":
        combos.sort(key=lambda c: (c.elapsed_hours, c.price))
    else:
        combos.sort(key=lambda c: (c.price, c.elapsed_hours))
    return combos


def print_combos(combos: list[Combo], top: int, *, show_links: bool) -> None:
    if not combos:
        print("No feasible combinations found.")
        return
    print(f"\n{'#':<3} {'Price':>9} {'Total':>8} {'Stop':>8} {'Hub':<22} Leg 1  ||  Leg 2")
    print("-" * 150)
    for i, c in enumerate(combos[:top], 1):
        chg = " ✈→✈ смена аэропорта" if c.airport_change else ""
        print(
            f"{i:<3} {c.price:>6.0f} {c.leg1.currency:<2} {fmt_hours(c.elapsed_hours):>8} {fmt_hours(c.stop_hours):>8} "
            f"{(c.hub_name + ' ' + c.country)[:22]:<22} "
            f"${c.leg1.price:.0f} {c.leg1.departure_at.strftime('%d.%m %H:%M')}→{c.leg1.arrival_at.strftime('%d.%m %H:%M')} "
            f"{c.leg1.route} {c.leg1.flights}  ||  "
            f"${c.leg2.price:.0f} {c.leg2.departure_at.strftime('%d.%m %H:%M')}→{c.leg2.arrival_at.strftime('%d.%m %H:%M')} "
            f"{c.leg2.route} {c.leg2.flights}{chg}"
        )
        if show_links:
            print(f"      leg1: {c.leg1.link}")
            print(f"      leg2: {c.leg2.link}")


# ----------------------------------------------------------------------------- CLI


def parse_dates(s: str) -> list[str]:
    dates = [d.strip() for d in s.split(",") if d.strip()]
    for d in dates:
        datetime.strptime(d, "%Y-%m-%d")  # validate early
    return dates


def main() -> int:
    p = argparse.ArgumentParser(description="Itinerary search via Aviasales GraphQL (Travelpayouts token)")
    sub = p.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("search", help="All one-way itineraries A->B on given dates, with flight numbers")
    s.add_argument("--from", dest="origin", required=True)
    s.add_argument("--to", dest="dest", required=True)
    s.add_argument("--dates", required=True, help="Comma-separated YYYY-MM-DD")
    s.add_argument("--direct", action="store_true", help="Non-stop only")
    s.add_argument("--max-hours", type=float, help="Drop itineraries longer than N hours door-to-door")
    s.add_argument("--max-price", type=float)
    s.add_argument("--sort", choices=["price", "duration"], default="price")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--currency", default="usd")
    s.add_argument("--market", default="ru", help="Aviasales market (ru, kz, us ...) — affects gates and prices")
    s.add_argument("--links", action="store_true", help="Print Aviasales deep links")
    s.add_argument("--json", dest="dump_json", action="store_true")

    st = sub.add_parser("stopover", help="A->HUB + HUB->B as two tickets with real connection check")
    st.add_argument("--from", dest="origin", required=True)
    st.add_argument("--to", dest="dest", required=True)
    st.add_argument("--dates", required=True, help="Leg 1 departure dates, comma-separated YYYY-MM-DD")
    st.add_argument("--hubs", nargs="+", help=f"Override hub list (default: {len(STOPOVER_HUBS)} curated hubs)")
    st.add_argument("--min-stop", type=float, default=16.0, help="Min hours between leg1 arrival and leg2 departure")
    st.add_argument("--max-stop", type=float, default=50.0, help="Max hours at the hub")
    st.add_argument("--min-connect-airport-change", type=float, default=4.0,
                    help="Min hours if leg2 departs from a different airport than leg1 arrives (e.g. DWC->DXB)")
    st.add_argument("--max-price", type=float, help="Budget for both legs")
    st.add_argument("--max-hours", type=float, help="Drop combos longer than N hours door-to-door")
    st.add_argument("--workers", type=int, default=1, help="Parallel hub queries (GraphQL rate limit is tight; 1-2)")
    st.add_argument("--exclude-countries", nargs="*", default=[], help="ISO codes of hub countries to skip")
    st.add_argument("--sort", choices=["price", "duration"], default="price")
    st.add_argument("--top", type=int, default=25)
    st.add_argument("--currency", default="usd")
    st.add_argument("--market", default="ru")
    st.add_argument("--links", action="store_true")
    st.add_argument("--json", dest="dump_json", action="store_true")

    args = p.parse_args()
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("ERROR: set TRAVELPAYOUTS_TOKEN env var", file=sys.stderr)
        return 2

    dates = parse_dates(args.dates)
    t0 = time.time()

    if args.mode == "search":
        its = run_search(
            token, args.origin.upper(), args.dest.upper(), dates,
            direct=True if args.direct else None, max_hours=args.max_hours, max_price=args.max_price,
            sort=args.sort, currency=args.currency, market=args.market,
        )
        print(f"Got {len(its)} itineraries in {time.time()-t0:.1f}s", file=sys.stderr)
        if args.dump_json:
            print(json.dumps([i.to_dict() for i in its[: args.top]], ensure_ascii=False, indent=2))
        else:
            print_itineraries(its, args.top, show_links=args.links)
    else:
        hubs = [h.upper() for h in (args.hubs or list(STOPOVER_HUBS))]
        combos = run_stopover(
            token, args.origin.upper(), args.dest.upper(), dates, hubs,
            min_stop=args.min_stop, max_stop=args.max_stop,
            min_connect_airport_change=args.min_connect_airport_change,
            max_price=args.max_price, max_hours=args.max_hours,
            exclude_countries={c.upper() for c in args.exclude_countries},
            sort=args.sort, currency=args.currency, market=args.market, workers=max(1, args.workers),
        )
        print(f"Got {len(combos)} feasible combos in {time.time()-t0:.1f}s", file=sys.stderr)
        if args.dump_json:
            print(json.dumps([c.to_dict() for c in combos[: args.top]], ensure_ascii=False, indent=2))
        else:
            print_combos(combos, args.top, show_links=args.links)
    return 0


if __name__ == "__main__":
    sys.exit(main())
