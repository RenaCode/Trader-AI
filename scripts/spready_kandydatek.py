"""Spread publicznych ksiag kandydatek Bramy 0 (renacode-infra: docs/projekty/trader/brama-0-gieldy.md).

Pelny spread (ask-bid)/mid w pb, mediana z N probek co 2 s, per gielda i aktywo
uniwersum (10 aktywow tradeable na bybit.eu). TYLKO publiczne endpointy, tylko
odczyt, zero kluczy. Wynik z 2026-10-11 17:48 UTC jest zapisany w
`koszty.KANDYDACI_BRAMY_0` - ten skrypt sluzy do powtorzenia odczytu, nie do
bramy: brama wymaga 30 dni WLASNYCH snapshotow, nie jednego odczytu z zewnatrz.

    python scripts/spready_kandydatek.py 12 > spready.json
"""
import json
import statistics
import sys
import time

AKTYWA = ["BTC", "ETH", "SOL", "DOGE", "XRP", "SUI", "ADA", "NEAR", "ARB", "LINK"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 12


import subprocess


def get(url):
    out = subprocess.run(["curl", "-s", "-m", "10", "-A", "spread-probe/1", url],
                         capture_output=True, text=True, timeout=15).stdout
    return json.loads(out)


def kraken_spot(a, q="EUR"):
    sym = {"BTC": "XBT", "DOGE": "XDG"}.get(a, a) + q
    d = get(f"https://api.kraken.com/0/public/Depth?pair={sym}&count=1")
    if d.get("error"):
        raise ValueError(d["error"])
    r = next(iter(d["result"].values()))
    return float(r["bids"][0][0]), float(r["asks"][0][0])


def kraken_perp(a):
    sym = "PF_" + {"BTC": "XBT"}.get(a, a) + "USD"
    d = get(f"https://futures.kraken.com/derivatives/api/v3/tickers/{sym}")
    t = d["ticker"]
    return float(t["bid"]), float(t["ask"])


def okx(a, q="USDC"):
    d = get(f"https://www.okx.com/api/v5/market/books?instId={a}-{q}&sz=1")
    b = d["data"][0]
    return float(b["bids"][0][0]), float(b["asks"][0][0])


def binance(a, q="USDC"):
    d = get(f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={a}{q}")
    return float(d["bidPrice"]), float(d["askPrice"])


def bitvavo(a):
    d = get(f"https://api.bitvavo.com/v2/{a}-EUR/book?depth=1")
    return float(d["bids"][0][0]), float(d["asks"][0][0])


def coinbase(a, q="EUR"):
    d = get(f"https://api.exchange.coinbase.com/products/{a}-{q}/book?level=1")
    return float(d["bids"][0][0]), float(d["asks"][0][0])


def kucoin(a, q="USDC"):
    d = get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={a}-{q}")
    x = d["data"]
    if not x:
        raise ValueError("brak")
    return float(x["bestBid"]), float(x["bestAsk"])


def bybit(host, a):
    d = get(f"https://{host}/v5/market/orderbook?category=spot&symbol={a}USDC&limit=1")
    r = d["result"]
    return float(r["b"][0][0]), float(r["a"][0][0])


def deribit(a):
    d = get(f"https://www.deribit.com/api/v2/public/ticker?instrument_name={a}_USDC-PERPETUAL")
    r = d["result"]
    return float(r["best_bid_price"]), float(r["best_ask_price"])


def bitget(a, q="USDC"):
    d = get(f"https://api.bitget.com/api/v2/spot/market/orderbook?symbol={a}{q}&limit=1")
    x = d["data"]
    return float(x["bids"][0][0]), float(x["asks"][0][0])


def cryptocom(a):
    d = get(f"https://api.crypto.com/exchange/v1/public/get-book?instrument_name={a}_USD&depth=1")
    x = d["result"]["data"][0]
    return float(x["bids"][0][0]), float(x["asks"][0][0])


GIELDY = {
    "bybit_eu_spot_USDC": lambda a: bybit("api.bybit.eu", a),
    "bybit_global_spot_USDC": lambda a: bybit("api.bybit.com", a),
    "kraken_spot_EUR": lambda a: kraken_spot(a, "EUR"),
    "kraken_spot_USD": lambda a: kraken_spot(a, "USD"),
    "kraken_perp_USD": kraken_perp,
    "okx_spot_USDC": lambda a: okx(a, "USDC"),
    "okx_spot_EUR": lambda a: okx(a, "EUR"),
    "okx_spot_USDT": lambda a: okx(a, "USDT"),
    "binance_spot_USDC": lambda a: binance(a, "USDC"),
    "binance_spot_EUR": lambda a: binance(a, "EUR"),
    "bitvavo_spot_EUR": bitvavo,
    "coinbase_spot_EUR": lambda a: coinbase(a, "EUR"),
    "coinbase_spot_USD": lambda a: coinbase(a, "USD"),
    "kucoin_spot_USDC": lambda a: kucoin(a, "USDC"),
    "kucoin_spot_USDT": lambda a: kucoin(a, "USDT"),
    "deribit_perp_USDC": deribit,
    "bitget_spot_USDC": lambda a: bitget(a, "USDC"),
    "cryptocom_spot_USD": cryptocom,
}

probki = {g: {a: [] for a in AKTYWA} for g in GIELDY}
bledy = {}
from concurrent.futures import ThreadPoolExecutor


def jedna_gielda(g):
    f = GIELDY[g]
    for _ in range(N):
        t0 = time.time()
        for a in AKTYWA:
            if (g, a) in bledy:
                continue
            try:
                bid, ask = f(a)
                if bid <= 0 or ask <= 0 or ask < bid:
                    raise ValueError(f"bid={bid} ask={ask}")
                probki[g][a].append((ask - bid) / ((ask + bid) / 2) * 1e4)
            except Exception as e:  # noqa: BLE001
                bledy[(g, a)] = str(e)[:80]
        time.sleep(max(0.0, 2.0 - (time.time() - t0)))
    print(f"{g}: gotowe", file=sys.stderr)


with ThreadPoolExecutor(max_workers=len(GIELDY)) as ex:
    list(ex.map(jedna_gielda, list(GIELDY)))

wynik = {}
for g in GIELDY:
    wynik[g] = {}
    for a in AKTYWA:
        p = probki[g][a]
        wynik[g][a] = (round(statistics.median(p), 3), len(p)) if p else None
    dostepne = [v[0] for v in wynik[g].values() if v]
    wynik[g]["_mediana_uniwersum"] = round(statistics.median(dostepne), 3) if dostepne else None
    wynik[g]["_srednia_uniwersum"] = round(statistics.mean(dostepne), 3) if dostepne else None
    wynik[g]["_n_aktywow"] = len(dostepne)
print(json.dumps({"N": N, "czas_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "wynik": wynik,
                  "bledy": {f"{g}/{a}": e for (g, a), e in bledy.items()}},
                 indent=1, ensure_ascii=False))
