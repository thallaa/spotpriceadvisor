#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API-based spot price advisor.

Re-implements the q15 advisor without any local database. Prices are fetched
from https://api.porssisahko.net/v2/latest-prices.json (quarter-hour data for
~48 h window). Output is a short Finnish text suitable for Siri/Shortcuts that
recommends the cheapest 3 h slot now, within the next 12 h, or across all
available future prices.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import locale
import os
import sys
import json
import urllib.request
import copy
import tomllib
import math

from flask import Flask, Response, abort, request

# Locale for Finnish month names in human_time
try:
    locale.setlocale(locale.LC_TIME, "fi_FI.UTF-8")
except Exception:
    pass

DEFAULT_TOKEN_SENTINEL = "CHANGEME_SPOTPRICE_TOKEN"

# Defaults can be overridden by /etc/spotpriceadvisor/config.toml or env
DEFAULT_CONFIG = {
    "server": {
        "token": os.environ.get("SPOTPRICE_TOKEN", DEFAULT_TOKEN_SENTINEL),  # empty -> no auth
        "port": int(os.environ.get("SPOTPRICE_PORT", "5000")),
    },
    "api": {
        "url": os.environ.get(
            "SPOTPRICE_API_URL", "https://api.porssisahko.net/v2/latest-prices.json"
        ),
        "timeout": 10,
        "user_agent": os.environ.get("SPOTPRICE_USER_AGENT", "spotpriceadvisor/1.0"),
    },
    "cache": {
        "enabled": os.environ.get("SPOTPRICE_CACHE", "false").lower() == "true",
        "redis_url": os.environ.get("SPOTPRICE_REDIS_URL", "redis://redis:6379/0"),
        "ttl_seconds": int(os.environ.get("SPOTPRICE_CACHE_TTL", "60")),
    },
}


def deep_merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    path = os.environ.get("SPOTPRICE_CONFIG", "/etc/spotpriceadvisor/config.toml")
    if os.path.isfile(path):
        try:
            with open(path, "rb") as fh:
                file_cfg = tomllib.load(fh)
            return deep_merge(DEFAULT_CONFIG, file_cfg)
        except Exception as exc:
            print(f"Warning: config load failed ({exc}), using defaults", file=sys.stderr)
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG


CONFIG = load_config()

TOKEN = CONFIG["server"].get("token") or ""
API_URL = CONFIG["api"]["url"]
API_TIMEOUT = CONFIG["api"]["timeout"]
API_UA = CONFIG["api"]["user_agent"]

# Force users to change the default token (or explicitly set empty)
if TOKEN == DEFAULT_TOKEN_SENTINEL and not os.environ.get("SPOTPRICE_ALLOW_DEFAULT", ""):
    raise RuntimeError(
        "SPOTPRICE_TOKEN not configured. Set SPOTPRICE_TOKEN (or config server.token) to a non-default value, "
        "or set it empty explicitly if you really want no auth."
    )

STRINGS = {
    "fi": {
        "past_avg": "Sähkön keskihinta viimeisen päivän ajalta on {avg} senttiä.",
        "cheap_current": "Seuraava {minutes}-minuuttinen jakso on halpa, vain {avg} senttiä.",
        "best_12h": "Seuraavan 12 tunnin sisällä halvimmillaan hinta on {avg} senttiä alkaen {time}.",
        "best_all": "Laajemmalla tarkastelulla halvimmillaan hinta on {avg} senttiä alkaen {time}.",
        "expensive": "Sähkö on melko kallista lähiaikoina, jaksoa ei ehkä kannata aloittaa vielä.",
        "best_now": "Halvin hetki on käytännössä nyt – {minutes}-minuuttinen kannattaa aloittaa heti.",
        "best_later": "Halvin {minutes}-minuuttinen alkaa {time}.",
        "today": "tänään",
        "tomorrow": "huomenna",
    },
    "en": {
        "past_avg": "The average electricity price over the last day is {avg} cents.",
        "cheap_current": "The next {minutes}-minute window is cheap, only {avg} cents.",
        "best_12h": "Within the next 12 hours the lowest price is {avg} cents starting {time}.",
        "best_all": "Looking further ahead, the lowest price is {avg} cents starting {time}.",
        "expensive": "Power is fairly expensive soon; you might want to wait.",
        "best_now": "The cheapest window is effectively now — start the {minutes}-minute window right away.",
        "best_later": "The cheapest {minutes}-minute window starts {time}.",
        "today": "today",
        "tomorrow": "tomorrow",
    },
    "sv": {
        "past_avg": "Medelpriset det senaste dygnet är {avg} cent.",
        "cheap_current": "Nästa {minutes}-minutersperiod är billig, bara {avg} cent.",
        "best_12h": "Inom de kommande 12 timmarna är lägsta priset {avg} cent med start {time}.",
        "best_all": "Vid bredare tidsram är lägsta priset {avg} cent med start {time}.",
        "expensive": "Elpriset är ganska högt just nu; vänta kanske med perioden.",
        "best_now": "Billigaste perioden är i princip nu – starta {minutes}-minutersperioden direkt.",
        "best_later": "Billigaste {minutes}-minutersperioden börjar {time}.",
        "today": "idag",
        "tomorrow": "imorgon",
    },
}

app = Flask(__name__)


def taxedprice_eur_per_kwh(net_eur_per_kwh: Decimal) -> Decimal:
    return net_eur_per_kwh * Decimal("1.255")


def round_snt(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def format_price(x: Decimal, lang: str) -> str:
    # Finnish and Swedish prefer decimal comma, English uses point
    s = f"{round_snt(x):.1f}"
    if lang in ("fi", "sv"):
        s = s.replace(".", ",")
    return s


def floor_to_q15(ts: int) -> int:
    return ts - (ts % 900)


MONTH_NAMES = {
    "fi": ["tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta", "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "sv": ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"],
}


def human_time(ts_epoch: int, lang: str) -> str:
    lang = lang if lang in STRINGS else "fi"
    dt_local = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).astimezone()
    today = datetime.now().astimezone().date()
    time_part = dt_local.strftime("%H:%M")
    if dt_local.date() == today:
        if lang == "en":
            return f"{STRINGS[lang]['today']} at {time_part}"
        if lang == "sv":
            return f"{STRINGS[lang]['today']} kl {time_part}"
        return f"{STRINGS[lang]['today']} kello {time_part}"
    if dt_local.date() == today + timedelta(days=1):
        if lang == "en":
            return f"{STRINGS[lang]['tomorrow']} at {time_part}"
        if lang == "sv":
            return f"{STRINGS[lang]['tomorrow']} kl {time_part}"
        return f"{STRINGS[lang]['tomorrow']} kello {time_part}"

    month = MONTH_NAMES[lang][dt_local.month - 1]
    if lang == "fi":
        return f"{dt_local.day}. {month} kello {time_part}"
    if lang == "sv":
        return f"{dt_local.day}. {month} kl {time_part}"
    return f"{month} {dt_local.day} at {time_part}"


def text_response(msg, status=200):
    return Response(msg, status=status, content_type="text/plain; charset=utf-8")


def fetch_api_prices(redis_client=None, cache_ttl: int = 0):
    """Return list of (start_ts_utc:int, gross_snt_per_kwh:Decimal). Uses Redis cache when provided."""
    cache_key = "spotpriceadvisor:latest-prices"

    if redis_client is not None:
        cached = redis_client.get(cache_key)
        if cached:
            try:
                raw_prices = json.loads(cached)
            except Exception:
                raw_prices = None
        else:
            raw_prices = None
    else:
        raw_prices = None

    if raw_prices is None:
        req = urllib.request.Request(API_URL, headers={"User-Agent": API_UA})
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                data = json.load(resp)
        except Exception as exc:
            raise RuntimeError(f"API-haku epäonnistui: {exc}") from exc

        raw_prices = data.get("prices")
        if not raw_prices:
            raise RuntimeError("API ei palauttanut hintatietoja.")

        if redis_client is not None and cache_ttl > 0:
            try:
                redis_client.setex(cache_key, cache_ttl, json.dumps(raw_prices))
            except Exception as exc:
                print(f"Warning: Redis-tallennus epäonnistui: {exc}", file=sys.stderr)

    out = []
    for item in raw_prices:
        # API price is given per kWh in euro cents (alv 0). Convert to €/kWh,
        # add tax, then back to cents for presentation.
        price_snt_net = Decimal(str(item["price"]))
        net_eur_per_kwh = price_snt_net / Decimal("100")
        gross_eur_per_kwh = taxedprice_eur_per_kwh(net_eur_per_kwh)
        gross_snt_per_kwh = gross_eur_per_kwh * Decimal("100")

        # startDate is ISO8601 in UTC (ends with Z)
        start_ts = int(datetime.fromisoformat(item["startDate"].replace("Z", "+00:00")).timestamp())
        out.append((start_ts, gross_snt_per_kwh))

    # Ensure ascending order by timestamp
    out.sort(key=lambda x: x[0])
    return out


def best_q15_window(ts_prices, window_q: int):
    """
    Sliding-window minimum for consecutive 15 min prices.
    Expects list of (ts, price) in ascending order with 900 s spacing.
    Returns (start_ts, avg_price) or None.
    """
    n = len(ts_prices)
    if n < window_q:
        return None

    prices = [p for _, p in ts_prices]
    run = sum(prices[:window_q])
    best_sum = run
    best_idx = 0
    for i in range(window_q, n):
        run += prices[i] - prices[i - window_q]
        if run < best_sum:
            best_sum = run
            best_idx = i - window_q

    return ts_prices[best_idx][0], best_sum / window_q


@app.route("/")
def advisor():
    auth = request.headers.get("Authorization", "")
    if TOKEN:
        if auth != f"Bearer {TOKEN}":
            abort(401)

    # Language and window length
    lang = request.args.get("lang", "fi").lower()
    if lang not in STRINGS:
        lang = "fi"
    try:
        duration_minutes = int(request.args.get("minutes", "180"))
    except ValueError:
        duration_minutes = 180
    duration_minutes = max(15, duration_minutes)
    window_q = math.ceil(duration_minutes / 15)

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    cur_q15_ts = floor_to_q15(now_ts)

    redis_client = None
    if CONFIG["cache"]["enabled"]:
        try:
            import redis

            redis_client = redis.from_url(CONFIG["cache"]["redis_url"])
        except Exception as exc:
            print(f"Warning: Redis-yhteys epäonnistui ({exc}), jatketaan ilman välimuistia.", file=sys.stderr)
            redis_client = None

    try:
        prices = fetch_api_prices(redis_client, CONFIG["cache"]["ttl_seconds"])
    except Exception as exc:
        return text_response(str(exc), 502)

    # Split past/future relative to current quarter start
    past = [p for ts, p in prices if ts < cur_q15_ts]
    future = [(ts, p) for ts, p in prices if ts >= cur_q15_ts]

    if not past:
        return text_response("Viimeisen vuorokauden hintatietoa ei ole saatavilla.", 503)
    if len(future) < window_q:
        return text_response("Tulevia varttihintoja ei ole riittävästi.", 503)

    past_avg = sum(past) / Decimal(len(past))

    current_prices = [p for _, p in future[:window_q]]
    current_avg = sum(current_prices) / Decimal(window_q)

    within_12h = future[:48] if len(future) >= 48 else future
    best_12h = best_q15_window(within_12h, window_q)
    best_all = best_q15_window(future, window_q)

    msg = []
    msg.append(STRINGS[lang]["past_avg"].format(avg=format_price(past_avg, lang)))

    if current_avg < past_avg * Decimal("0.90"):
        msg.append(
            STRINGS[lang]["cheap_current"].format(
                minutes=duration_minutes, avg=format_price(current_avg, lang)
            )
        )

    if best_12h and best_12h[1] < current_avg:
        start12, avg12 = best_12h
        msg.append(
            STRINGS[lang]["best_12h"].format(
                avg=format_price(avg12, lang), time=human_time(start12, lang)
            )
        )

    if best_all and (best_12h is None or best_all[1] < best_12h[1]):
        start_all, avg_all = best_all
        msg.append(
            STRINGS[lang]["best_all"].format(
                avg=format_price(avg_all, lang), time=human_time(start_all, lang)
            )
        )

    candidates = [
        (cur_q15_ts, current_avg, "current"),
    ]
    if best_12h:
        candidates.append((best_12h[0], best_12h[1], "12h"))
    if best_all:
        candidates.append((best_all[0], best_all[1], "future"))

    best_start, best_avg, best_label = min(candidates, key=lambda x: x[1])
    best_human = human_time(best_start, lang)

    if best_avg >= past_avg * Decimal("0.90"):
        msg.append(STRINGS[lang]["expensive"])
    elif best_label == "current":
        msg.append(
            STRINGS[lang]["best_now"].format(
                minutes=duration_minutes, time=best_human
            )
        )
    else:
        msg.append(
            STRINGS[lang]["best_later"].format(
                minutes=duration_minutes, time=best_human
            )
        )

    return text_response(" ".join(msg))


if __name__ == "__main__":
    app.run(port=CONFIG["server"]["port"])
