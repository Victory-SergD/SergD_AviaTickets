#!/usr/bin/env python3
"""
Travelpayouts (Aviasales) flight search CLI — same interface shape as duffel_search.py.

Uses the Aviasales Data API v3 (cached prices) — fast, real prices, no KYC.

Examples:
  # cheapest from Ulaanbaatar to any major Russian city
  python travelpayouts_search.py --from ULN --to RU --date 2026-05-19 --sort price

  # cheapest to Moscow (city code MOW covers SVO/DME/VKO)
  python travelpayouts_search.py --from ULN --to MOW --date 2026-05-19 --sort price

  # with 2-day stopover via various hubs
  python travelpayouts_search.py --stopover --from ULN --to MOW --depart 2026-05-19 --stop-days 2

  # direct only
  python travelpayouts_search.py --from ULN --to MOW --date 2026-05-19 --direct

Environment:
  TRAVELPAYOUTS_TOKEN — your API token from https://www.travelpayouts.com (Tools → API)
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

API_BASE = "https://api.travelpayouts.com"

# Russian destinations (TP supports city codes that aggregate multiple airports — MOW = SVO+DME+VKO, LED, etc.)
RU_DESTS = {
    "MOW": "Moscow (all airports)",
    "LED": "St Petersburg",
    "OVB": "Novosibirsk",
    "IKT": "Irkutsk",
    "UUD": "Ulan-Ude",
    "KJA": "Krasnoyarsk",
    "VVO": "Vladivostok",
    "KZN": "Kazan",
    "AER": "Sochi",
    "SVX": "Yekaterinburg",
    "ROV": "Rostov-on-Don",
}

STOPOVER_HUBS = {
    "ICN": "Seoul",
    "PEK": "Beijing",
    "PVG": "Shanghai",
    "HKG": "Hong Kong",
    "BKK": "Bangkok",
    "SIN": "Singapore",
    "DXB": "Dubai",
    "DOH": "Doha",
    "IST": "Istanbul",
    "AUH": "Abu Dhabi",
    "ALA": "Almaty",
    "TAS": "Tashkent",
    "FRU": "Bishkek",
    "TBS": "Tbilisi",
    "EVN": "Yerevan",
}


def fmt_duration(minutes: int) -> str:
    h, m = divmod(int(minutes or 0), 60)
    return f"{h}h{m:02d}m"


@dataclass
class Offer:
    price: float
    currency: str
    duration_min: int
    airline: str
    flight_number: str
    departure_at: str
    arrival_at: str
    transfers: int
    origin: str
    destination: str
    leg2: dict[str, Any] | None  # for stopover combos
    link: str

    @property
    def slices_summary(self) -> str:
        dep = self.departure_at[:16].replace("T", " ")
        if self.leg2:
            dep2 = self.leg2["departure_at"][:16].replace("T", " ")
            return (
                f"{self.origin} > {self.destination} {dep} ({self.transfers}st) "
                f"| {self.leg2['origin']} > {self.leg2['destination']} {dep2} "
                f"({self.leg2['transfers']}st)"
            )
        return f"{self.origin} > {self.destination} {dep} ({self.transfers} stops)"


def search_one(
    token: str,
    origin: str,
    destination: str,
    departure_at: str,
    *,
    direct: bool = False,
    currency: str = "usd",
    one_way: bool = True,
    return_at: str | None = None,
    sorting: str = "price",
    limit: int = 30,
    timeout: float = 30.0,
) -> list[Offer]:
    """Query /aviasales/v3/prices_for_dates — cached cheap fares."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": departure_at,
        "one_way": str(one_way).lower(),
        "direct": str(direct).lower(),
        "currency": currency,
        "sorting": sorting,
        "limit": str(limit),
        "token": token,
    }
    if return_at:
        params["return_at"] = return_at
        params["one_way"] = "false"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(
            f"{API_BASE}/aviasales/v3/prices_for_dates", params=params
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"TP {resp.status_code} for {origin}->{destination}: {resp.text[:300]}"
            )
        payload = resp.json()
        if not payload.get("success", True):
            raise RuntimeError(f"TP error: {json.dumps(payload)[:300]}")
        results = []
        for item in payload.get("data", []):
            results.append(
                Offer(
                    price=float(item["price"]),
                    currency=payload.get("currency", currency).upper(),
                    duration_min=int(item.get("duration", 0)),
                    airline=item.get("airline", "?"),
                    flight_number=item.get("flight_number", ""),
                    departure_at=item["departure_at"],
                    arrival_at=item.get("arrival_at", ""),
                    transfers=int(item.get("transfers", 0)),
                    origin=item.get("origin_airport", item["origin"]),
                    destination=item.get("destination_airport", item["destination"]),
                    leg2=None,
                    link=item.get("link", ""),
                )
            )
        return results


def search_stopover(
    token: str,
    origin: str,
    destination: str,
    depart_date: str,
    stop_days: int,
    hubs: list[str],
    *,
    currency: str = "usd",
    direct_legs: bool = False,
    workers: int = 6,
) -> list[Offer]:
    """For each hub: search ORIG->HUB and HUB->DEST separately, then combine cheapest."""
    leg2_date = (datetime.strptime(depart_date, "%Y-%m-%d") + timedelta(days=stop_days)).strftime("%Y-%m-%d")
    combos: list[Offer] = []
    errors: list[str] = []

    def fetch(hub: str) -> tuple[str, list[Offer], list[Offer]]:
        leg1 = search_one(token, origin, hub, depart_date, direct=direct_legs, currency=currency, limit=10)
        leg2 = search_one(token, hub, destination, leg2_date, direct=direct_legs, currency=currency, limit=10)
        return hub, leg1, leg2

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, h): h for h in hubs if h not in (origin, destination)}
        for fut in as_completed(futures):
            hub = futures[fut]
            try:
                hub_name, leg1s, leg2s = fut.result()
                if not leg1s or not leg2s:
                    continue
                # pick cheapest leg1 and cheapest leg2 for this hub
                l1 = min(leg1s, key=lambda o: o.price)
                l2 = min(leg2s, key=lambda o: o.price)
                combined = Offer(
                    price=l1.price + l2.price,
                    currency=l1.currency,
                    duration_min=l1.duration_min + l2.duration_min,
                    airline=f"{l1.airline}/{l2.airline}",
                    flight_number=f"{l1.flight_number}+{l2.flight_number}",
                    departure_at=l1.departure_at,
                    arrival_at=l2.arrival_at,
                    transfers=l1.transfers + l2.transfers,
                    origin=l1.origin,
                    destination=l1.destination,
                    leg2={
                        "origin": l2.origin,
                        "destination": l2.destination,
                        "departure_at": l2.departure_at,
                        "transfers": l2.transfers,
                    },
                    link=f"{l1.link} | {l2.link}",
                )
                combos.append(combined)
            except Exception as e:
                errors.append(f"{hub}: {e}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    return combos


def print_table(offers: list[Offer], limit: int = 15) -> None:
    if not offers:
        print("No offers found.")
        return
    print(f"\n{'#':<3} {'Price':>10} {'Duration':>10} {'Stops':>5}  {'Airline':<14} Route")
    print("-" * 120)
    for i, o in enumerate(offers[:limit], 1):
        print(
            f"{i:<3} {o.price:>7.2f} {o.currency:<2} "
            f"{fmt_duration(o.duration_min):>10} "
            f"{o.transfers:>5}  {o.airline[:14]:<14} {o.slices_summary}"
        )


def main() -> int:
    p = argparse.ArgumentParser(description="Travelpayouts flight search")
    p.add_argument("--from", dest="origin", help="Origin IATA (city or airport)")
    p.add_argument("--to", dest="dest", help="Dest IATA or 'RU' for major RU cities")
    p.add_argument("--date", help="Departure YYYY-MM-DD (or YYYY-MM for month sweep)")
    p.add_argument("--return-date")
    p.add_argument("--direct", action="store_true", help="Direct flights only")
    p.add_argument("--currency", default="usd")
    p.add_argument("--sort", choices=["price", "duration"], default="price")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--stopover", action="store_true")
    p.add_argument("--depart", help="(stopover) leg 1 date")
    p.add_argument("--stop-days", type=int, default=2)
    p.add_argument("--hubs", nargs="+")
    p.add_argument("--json", dest="dump_json", action="store_true")
    args = p.parse_args()

    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("ERROR: set TRAVELPAYOUTS_TOKEN env var", file=sys.stderr)
        return 2

    t0 = time.time()
    offers: list[Offer] = []
    errors: list[str] = []

    if args.stopover:
        if not (args.origin and args.dest and args.depart):
            print("--stopover requires --from --to --depart", file=sys.stderr)
            return 2
        hubs = args.hubs or list(STOPOVER_HUBS.keys())
        print(f"Searching via {len(hubs)} hubs...", file=sys.stderr)
        offers = search_stopover(
            token, args.origin, args.dest, args.depart, args.stop_days, hubs,
            currency=args.currency, direct_legs=args.direct,
        )
    else:
        if not (args.origin and args.date):
            print("--from and --date required", file=sys.stderr)
            return 2
        dests = [args.dest] if args.dest and args.dest != "RU" else list(RU_DESTS.keys())
        dests = [d for d in dests if d != args.origin]
        print(f"Searching {len(dests)} destination(s)...", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                ex.submit(
                    search_one, token, args.origin, d, args.date,
                    direct=args.direct, currency=args.currency,
                    return_at=args.return_date, limit=10,
                ): d
                for d in dests
            }
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    offers.extend(fut.result())
                except Exception as e:
                    errors.append(f"{d}: {e}")

    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    print(f"Got {len(offers)} offers in {time.time()-t0:.1f}s", file=sys.stderr)

    if args.sort == "price":
        offers.sort(key=lambda o: o.price)
    else:
        offers.sort(key=lambda o: o.duration_min or 10**9)

    print_table(offers, limit=args.limit)

    if args.dump_json and offers:
        print("\n--- top offer ---")
        top = offers[0]
        print(json.dumps({
            "price": top.price, "currency": top.currency,
            "airline": top.airline, "flight_number": top.flight_number,
            "departure_at": top.departure_at, "arrival_at": top.arrival_at,
            "transfers": top.transfers, "duration_min": top.duration_min,
            "link": top.link,
        }, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
