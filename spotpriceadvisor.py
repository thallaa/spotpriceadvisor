#!/usr/bin/env python3
from flask import Flask, request, abort
from datetime import datetime, timedelta, timezone
import MySQLdb
import os
import locale
from flask import make_response
from flask import Response
from itertools import islice

# Set locale to Finnish for month names
locale.setlocale(locale.LC_TIME, "fi_FI.UTF-8")

# Config
DB = {
"host": "localhost",
"user": "elspot",
"passwd": "elspotpwd2",
"db": "elspot_utc",
}
TOKEN = os.environ.get("SPOTPRICE_TOKEN", "spotpriceadvisorpwd")

app = Flask(__name__)

def taxedprice(taxfree):
    return 1.255 * (float(taxfree))
#    return 3.169 + 1.24000386 * (float(taxfree) - 2.556)

def calculate_avg_price(prices):
    ps = []
    for kvp in prices:
        ps.append(kvp[1])
    return sum(ps) / len(ps)


def price_significantly_less(new_price, og_price):
    if new_price < og_price * 0.9:
        return True
    return False


def find_lowest_3h_price_in_hr_range(prices, starting_h, ending_h):
    lowest_price = 9999999
    lowest_starting_hour = 0

    for i in range(starting_h, ending_h-2):
        following_prices = prices[i:i+3]
        following_avg_prices = calculate_avg_price(following_prices)
        if following_avg_prices < lowest_price:
            lowest_price = following_avg_prices
            lowest_starting_hour = i

    return lowest_price, lowest_starting_hour


def human_time(ts):
    dt_local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    today = datetime.now().astimezone().date()
    if dt_local.date() == today:
        return f"tänään kello {dt_local.strftime('%H')}"
    elif dt_local.date() == today + timedelta(days=1):
        return f"huomenna kello {dt_local.strftime('%H')}"
    else:
        return f"{dt_local.day}. {dt_local.strftime('%B')}ta kello {dt_local.strftime('%H')}"


def text_response(msg, status=200):
    return Response(msg, status=status, content_type="text/plain; charset=utf-8")


@app.route("/")
def advisor():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {TOKEN}":
        abort(401)

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    cur_ts = now_ts - (now_ts % 3600)
    week_ago_ts = now_ts - 7 * 86400

    try:
        db = MySQLdb.connect(**DB)
        cur = db.cursor()

        # Fetch all relevant prices from past 7 days and future (up to 48h)
        cur.execute("SELECT timestamp, price FROM spotprice WHERE timestamp >= %s ORDER BY timestamp", (week_ago_ts,))
        rows = cur.fetchall()
        db.close()
    except Exception as e:
        return text_response(f"Tietokantavirhe: {e}", 500)

    # Build timestamp → taxed price map, also convert from €/MWh to ¢/kWh
    prices = {ts: taxedprice(price/10) for ts, price in rows}

    # Future timestamps
    future_prices = [[ts, prices[ts]] for ts in sorted(ts for ts in prices if ts >= now_ts)]

    # Current hour price
    curprice = prices.get(cur_ts)
    if curprice is None:
        return text_response("Tämän hetken hintatietoa ei ole saatavilla.")

    # Last 7 days avg price (from past timestamps only)
    past_prices = [p for ts, p in prices.items() if ts < now_ts]
    if len(past_prices) == 0:
        return text_response("Viime seitsemän päivän ajalta ei ole saatavilla hintatietoa.")
    past_7d_avg_price = sum(past_prices) / len(past_prices)

    # ----------- FIND LOWEST PRICE ----------

    price_candidates = []
    text_response_str = f"Sähkön hinnan keskiarvo viime seitsemän päivän ajalta on "
    text_response_str += f"{past_7d_avg_price:.1f}".replace('.', ',')
    text_response_str += f" senttiä. "

    # check if the price of electricity is currently lower than the avg of the past 7 days


    # ------------- get price of electricity in the following 3 hour window --------------
    # compare it to avg price
    future_3h_prices = future_prices[0:3]
    future_3h_avg_price = calculate_avg_price(future_3h_prices)
    price_candidates.append((future_3h_avg_price, 0, "current"))
    if price_significantly_less(future_3h_avg_price, past_7d_avg_price):
        text_response_str += f"Sähkö on seuraavan kolmen tunnin aikana halpaa, vain "
        text_response_str += f"{future_3h_avg_price:.1f}".replace('.', ',')
        text_response_str += f" senttiä. "

    future_prices_starting_idx = 1

    # ------------- get lowest price of electricity in the following 12 hours if there's enough data ------------
    if len(future_prices) >= 12:
        # check prices for next 12 hours and all data separately
        # so first 12 hours and then check all data, set starting index + 12

        lowest_price_in_12h, lowest_starting_hour_in_12 = find_lowest_3h_price_in_hr_range(future_prices, 1, 12)
        price_candidates.append((lowest_price_in_12h, lowest_starting_hour_in_12, "12h"))
        if price_significantly_less(lowest_price_in_12h, past_7d_avg_price and lowest_price_in_12h < future_3h_avg_price):
            text_response_str += f"Sähkö on seuraavan kahdentoista tunnin aikana halvimmillaan "
            text_response_str += f"{lowest_price_in_12h:.1f}".replace('.', ',')
            text_response_str += f" senttiä {lowest_starting_hour_in_12} tunnin päästä. "

        future_prices_starting_idx = 12

    # ----------------- get lowest price of electricity in all future data --------------------------
    lowest_price_in_future, lowest_starting_hour_in_future = find_lowest_3h_price_in_hr_range(future_prices, future_prices_starting_idx, len(future_prices))
    price_candidates.append((lowest_price_in_future, lowest_starting_hour_in_future, "future"))
    if price_significantly_less(lowest_price_in_future, past_7d_avg_price) and lowest_price_in_future < lowest_price_in_12h and lowest_price_in_future < future_3h_avg_price:
        text_response_str += f"Sähkö on kuitenkin halvimmillaan "
        text_response_str += f"{lowest_price_in_future:.1f}".replace('.', ',')
        text_response_str += f" senttiä {lowest_starting_hour_in_future} tunnin päästä. "


    # ------------- find best electricity price -----------------
    best_price, best_hour, best_label = min(price_candidates, key=lambda x: x[0])

    best_ts = cur_ts + best_hour * 3600
    best_human_time = human_time(best_ts)

    if not price_significantly_less(best_price, past_7d_avg_price):
        text_response_str += "Sähkö on melko kallista lähiaikoina eli pesua ei kannata aloittaa vielä."

    elif best_label == "current":
        text_response_str += "Sähkö on halvimmillaan nyt eli pesu kannattaa aloittaa heti."

    elif best_label == "12h" or best_label == "future":
        text_response_str += f"Sähkö on siis halvimmillaan {best_human_time}."

    return text_response(text_response_str)

if __name__ == "__main__":
    app.run(port=5000)
