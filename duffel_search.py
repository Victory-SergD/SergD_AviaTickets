#!/usr/bin/env python3
"""
Duffel flight search CLI.

Usage examples:
  # cheapest one-way from Ulaanbaatar to any major Russian airport
  python duffel_search.py --from ULN --to RU --date 2026-05-18 --sort price

  # fastest, business class, 1 stop max
  python duffel_search.py --from ULN --to SVO --date 2026-05-18 --sort duration --max-stops 1

  # multi-city: ULN -> ICN (2-day stop) -> SVO
  python duffel_search.py --multi ULN:ICN:2026-05-18 ICN:SVO:2026-05-20

  # cheapest with a 2-day stopover through any of the candidate hubs
  python duffel_search.py --stopover --from ULN --to SVO --depart 2026-05-18 --stop-days 2

Environment:
  DUFFEL_TOKEN  - your Duffel access token (test or live)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

API_BASE = "https://api.duffel.com"
API_VERSION = "v2"

# Major Russian airports we care about by default
RU_AIRPORTS = {
    "SVO": "Moscow Sheremetyevo",
    "DME": "Moscow Domodedovo",
    "VKO": "Moscow Vnukovo",
    "LED": "St Petersburg Pulkovo",
    "OVB": "Novosibirsk",
    "IKT": "Irkutsk",
    "UUD": "Ulan-Ude",
    "KJA": "Krasnoyarsk",
    "VVO": "Vladivostok",
    "KZN": "Kazan",
    "AER": "Sochi",
}

# Likely stopover hubs reachable from Mongolia / Russia.
# Note: Duffel rejects FRU (Bishkek) as of 2026-05 — not in their catalogue.
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
}


def parse_iso_duration(d: str) -> int:
    """ISO 8601 duration like 'PT12H30M' -> minutes."""
    if not d:
        return 0
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", d)
    if not m:
        return 0
    days, hours, mins = (int(x) if x else 0 for x in m.groups())
    return days * 24 * 60 + hours * 60 + mins


def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m"


@dataclass
class Offer:
    id: str
    price: float
    currency: str
    duration_min: int
    owner: str
    slices_summary: str
    stops: int
    raw: dict[str, Any]


def summarize_slices(slices: list[dict[str, Any]]) -> tuple[str, int]:
    parts = []
    total_stops = 0
    for sl in slices:
        segs = sl["segments"]
        total_stops += max(0, len(segs) - 1)
        chain = " > ".join(
            [segs[0]["origin"]["iata_code"]]
            + [s["destination"]["iata_code"] for s in segs]
        )
        dep = segs[0]["departing_at"][:16].replace("T", " ")
        arr = segs[-1]["arriving_at"][:16].replace("T", " ")
        parts.append(f"{chain}  {dep} -> {arr}")
    return " | ".join(parts), total_stops


def offer_from_raw(raw: dict[str, Any]) -> Offer:
    summary, stops = summarize_slices(raw["slices"])
    return Offer(
        id=raw["id"],
        price=float(raw["total_amount"]),
        currency=raw["total_currency"],
        duration_min=sum(parse_iso_duration(s.get("duration", "")) for s in raw["slices"]),
        owner=raw["owner"]["name"],
        slices_summary=summary,
        stops=stops,
        raw=raw,
    )


def search(
    token: str,
    slices: list[dict[str, str]],
    *,
    pax: int = 1,
    cabin: str = "economy",
    max_connections: int | None = None,
    timeout: float = 60.0,
) -> list[Offer]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Duffel-Version": API_VERSION,
    }
    body = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"}] * pax,
            "cabin_class": cabin,
        }
    }
    params = {"return_offers": "true"}
    if max_connections is not None:
        params["supplier_timeout"] = "20000"

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{API_BASE}/air/offer_requests",
            headers=headers,
            params=params,
            json=body,
        )
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text}
            raise RuntimeError(
                f"Duffel {resp.status_code} for {slices}: {json.dumps(err)[:500]}"
            )
        data = resp.json()["data"]
        offers = data.get("offers", [])
        result = [offer_from_raw(o) for o in offers]
        if max_connections is not None:
            result = [o for o in result if o.stops <= max_connections]
        return result


def search_many(
    token: str,
    slice_sets: list[list[dict[str, str]]],
    *,
    pax: int,
    cabin: str,
    max_connections: int | None,
    workers: int = 4,
) -> list[Offer]:
    all_offers: list[Offer] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(search, token, s, pax=pax, cabin=cabin, max_connections=max_connections): s
            for s in slice_sets
        }
        for fut in as_completed(futures):
            slc = futures[fut]
            try:
                all_offers.extend(fut.result())
            except Exception as e:
                errors.append(f"{slc[0]['origin']}->{slc[-1]['destination']}: {e}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    return all_offers


def print_table(offers: list[Offer], limit: int = 15) -> None:
    if not offers:
        print("No offers found.")
        return
    print(f"\n{'#':<3} {'Price':>12} {'Duration':>10} {'Stops':>5}  {'Carrier':<22} Route")
    print("-" * 110)
    for i, o in enumerate(offers[:limit], 1):
        print(
            f"{i:<3} {o.price:>9.2f} {o.currency:<2} "
            f"{fmt_duration(o.duration_min):>10} "
            f"{o.stops:>5}  {o.owner[:22]:<22} {o.slices_summary}"
        )


def add_days(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def main() -> int:
    p = argparse.ArgumentParser(description="Duffel flight search")
    p.add_argument("--from", dest="origin", help="Origin IATA (e.g. ULN)")
    p.add_argument(
        "--to",
        dest="dest",
        help="Destination IATA, or 'RU' for all major Russian airports",
    )
    p.add_argument("--date", help="Departure date YYYY-MM-DD")
    p.add_argument("--return-date", help="Return date YYYY-MM-DD (optional)")
    p.add_argument("--pax", type=int, default=1)
    p.add_argument("--cabin", default="economy", choices=["economy", "premium_economy", "business", "first"])
    p.add_argument("--max-stops", type=int, default=None)
    p.add_argument("--sort", choices=["price", "duration"], default="price")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument(
        "--multi",
        nargs="+",
        help="Multi-city slices as ORIG:DEST:YYYY-MM-DD pairs",
    )
    p.add_argument(
        "--stopover",
        action="store_true",
        help="Search routes with a multi-day stop via various hubs",
    )
    p.add_argument("--depart", help="(stopover mode) leg 1 departure date")
    p.add_argument("--stop-days", type=int, default=2)
    p.add_argument(
        "--hubs",
        nargs="+",
        help="(stopover mode) override stopover candidates list",
    )
    p.add_argument("--json", dest="dump_json", action="store_true", help="Print raw JSON of top offer")
    args = p.parse_args()

    token = os.environ.get("DUFFEL_TOKEN")
    if not token:
        print("ERROR: set DUFFEL_TOKEN env var (test token starts with duffel_test_)", file=sys.stderr)
        return 2

    # Build slice sets
    slice_sets: list[list[dict[str, str]]] = []
    labels: list[str] = []

    if args.multi:
        slc = []
        for piece in args.multi:
            o, d, dt = piece.split(":")
            slc.append({"origin": o, "destination": d, "departure_date": dt})
        slice_sets.append(slc)
        labels.append("multi-city")
    elif args.stopover:
        if not (args.origin and args.dest and args.depart):
            print("--stopover requires --from, --to, --depart", file=sys.stderr)
            return 2
        hubs = args.hubs or list(STOPOVER_HUBS.keys())
        leg2_date = add_days(args.depart, args.stop_days)
        for hub in hubs:
            if hub in (args.origin, args.dest):
                continue
            slice_sets.append(
                [
                    {"origin": args.origin, "destination": hub, "departure_date": args.depart},
                    {"origin": hub, "destination": args.dest, "departure_date": leg2_date},
                ]
            )
            labels.append(f"via {hub}")
    else:
        if not (args.origin and args.date):
            print("--from and --date are required (or use --multi / --stopover)", file=sys.stderr)
            return 2
        dests = [args.dest] if args.dest and args.dest != "RU" else list(RU_AIRPORTS.keys())
        for d in dests:
            if d == args.origin:
                continue
            one = [{"origin": args.origin, "destination": d, "departure_date": args.date}]
            if args.return_date:
                one.append({"origin": d, "destination": args.origin, "departure_date": args.return_date})
            slice_sets.append(one)
            labels.append(f"{args.origin}->{d}")

    print(f"Searching {len(slice_sets)} route(s)...", file=sys.stderr)
    t0 = time.time()
    offers = search_many(
        token, slice_sets,
        pax=args.pax, cabin=args.cabin, max_connections=args.max_stops,
    )
    print(f"Got {len(offers)} offers in {time.time()-t0:.1f}s", file=sys.stderr)

    if args.sort == "price":
        offers.sort(key=lambda o: o.price)
    else:
        offers.sort(key=lambda o: o.duration_min)

    print_table(offers, limit=args.limit)

    if args.dump_json and offers:
        print("\n--- top offer JSON ---")
        print(json.dumps(offers[0].raw, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
