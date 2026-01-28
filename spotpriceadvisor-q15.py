#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, abort, Response
from datetime import datetime, timedelta, timezone
import MySQLdb
import os
import locale
from decimal import Decimal, ROUND_HALF_UP

# Set Finnish locale
try:
    locale.setlocale(locale.LC_TIME, "fi_FI.UTF-8")
except Exception:
    pass

DB = {
    "host": "localhost",
    "user": "elspot",
    "passwd": "elspotpwd2",
    "db": "elspot_utc",
}
TOKEN = os.environ.get("SPOTPRICE_TOKEN", "spotpriceadvisorpwd")

# 3h = 12 qurters
WINDOW_Q = 12

app = Flask(__name__)

def compute_future_horizon_q(now_utc: datetime, cur_q15_ts: int) -> int:
    """
    Palauttaa, montako varttia haetaan tulevaisuudesta.
    - Ennen klo 14 (paikallisaikaa): vähintään 24 h (96 varttia)
    - Klo 14 jälkeen: haetaan huomisen loppuun (23:45) asti
    """
    local_now = now_utc.astimezone()  # palvelimen paikallinen aikavyöhyke (FI)
    base_quarters = 96  # 24 h

    if local_now.hour < 14:
        return base_quarters

    # Klo 14 jälkeen: laske "huomisen loppu" paikallisajassa (23:45)
    tomorrow = local_now.date() + timedelta(days=1)
    # seuraavan päivän puoliyö + 23:45 = huomisen loppu
    end_of_tomorrow_local = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time(), tzinfo=local_now.tzinfo) - timedelta(minutes=15)
    end_of_tomorrow_utc = end_of_tomorrow_local.astimezone(timezone.utc)
    end_ts = int(end_of_tomorrow_utc.timestamp())

    # Kuinka monta 15 min jaksoa nykyisestä vartista tuohon hetkeen (mukana molemmat päät)
    quarters = max(base_quarters, ((end_ts - cur_q15_ts) // 900) + 1)
    return int(quarters)

def taxedprice_eur_per_kwh(net_eur_per_kwh: Decimal) -> Decimal:
    return (net_eur_per_kwh * Decimal("1.255"))

def round_snt(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

def floor_to_q15(ts: int) -> int:
    # Floor down to beginning of 15 min
    return ts - (ts % 900)

def human_time(ts_epoch: int) -> str:
    # Tulosta “tänään/huomenna klo HH:MM” tai “D. Kuukautta klo HH:MM”
    dt_local = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).astimezone()
    today = datetime.now().astimezone().date()
    if dt_local.date() == today:
        return f"tänään kello {dt_local.strftime('%H:%M')}"
    elif dt_local.date() == today + timedelta(days=1):
        return f"huomenna kello {dt_local.strftime('%H:%M')}"
    else:
        # Esim: "2. lokakuuta kello 13:45"
        day = dt_local.day
        month_name = dt_local.strftime('%B')
        return f"{day}. {month_name}ta kello {dt_local.strftime('%H:%M')}"

def text_response(msg, status=200):
    return Response(msg, status=status, content_type="text/plain; charset=utf-8")

def fetch_q15_prices_since(db_cur, ts_from: int, limit: int = None):
    """
    Palauttaa listan (ts_epoch, taxed_cents_per_kwh) aikajärjestyksessä.
    DB price on snt/kWh veroton -> muunnetaan euroiksi ja verolliseksi, sitten takaisin snt/kWh esitystä varten.
    """
    if limit is not None:
        db_cur.execute(
            """SELECT timestamp, price
               FROM spotprice_q15
               WHERE timestamp >= %s
               ORDER BY timestamp ASC
               LIMIT %s""",
            (ts_from, limit),
        )
    else:
        db_cur.execute(
            """SELECT timestamp, price
               FROM spotprice_q15
               WHERE timestamp >= %s
               ORDER BY timestamp ASC""",
            (ts_from,),
        )
    rows = db_cur.fetchall()
    out = []
    for ts, price_snt_net in rows:
        # snt/kWh (veroton) -> €/kWh (veroton)
        net_eur_per_kwh = Decimal(str(price_snt_net)) / Decimal("1000")
        # verollinen €/kWh
        gross_eur_per_kwh = taxedprice_eur_per_kwh(net_eur_per_kwh)
        # esitetään snt/kWh:na
        gross_snt_per_kwh = gross_eur_per_kwh * Decimal("100")
        out.append((int(ts), gross_snt_per_kwh))
    return out

def fetch_past_7d_prices(db_cur, now_ts: int):
    week_ago = now_ts - 7 * 86400
    db_cur.execute(
        """SELECT timestamp, price
           FROM spotprice_q15
           WHERE timestamp >= %s AND timestamp < %s
           ORDER BY timestamp ASC""",
        (week_ago, now_ts),
    )
    rows = db_cur.fetchall()
    out = []
    for ts, price_snt_net in rows:
        net_eur = Decimal(str(price_snt_net)) / Decimal("1000")
        gross_eur = taxedprice_eur_per_kwh(net_eur)
        out.append(gross_eur * Decimal("100"))  # snt/kWh verollinen
    return out

def best_q15_window(ts_prices, window_q: int):
    """
    Liukuva ikkuna peräkkäisille 15 min hinnoille.
    ts_prices: lista (ts, snt_gross) aikajärjestyksessä, oletus: 900 s välein.
    Palauttaa (start_ts, avg_snt_over_window) tai None jos dataa liian vähän.
    """
    n = len(ts_prices)
    if n < window_q:
        return None
    # varmistetaan peräkkäisyys (hyvin kevyt tarkistus):
    # jos havaitaan hyppäys != 900 s, katkaistaan ennen hyppyä jotta ei valita rikkinäistä ikkunaa
    clean = []
    prev_ts = None
    for ts, price in ts_prices:
        if prev_ts is not None and ts - prev_ts != 900:
            # aloitamme uuden ketjun, koska väli ei ole yhtenäinen
            # tallennetaan "katkaisu" markerina None: käsitellään ketjut erikseen
            clean.append((None, None))
        clean.append((ts, price))
        prev_ts = ts

    # etsi paras ikkuna jokaisesta yhtenäisestä ketjusta erikseen
    best = None
    segment = []
    for item in clean + [(None, None)]:  # lisätään päätösmarker
        if item[0] is None:
            # käsittele segmentti
            if len(segment) >= window_q:
                # laske liukuva summa
                prices = [p for _, p in segment]
                run = sum(prices[:window_q])
                best_sum = run
                best_idx = 0
                for i in range(window_q, len(prices)):
                    run += prices[i] - prices[i - window_q]
                    if run < best_sum:
                        best_sum = run
                        best_idx = i - window_q
                avg = best_sum / window_q
                start_ts = segment[best_idx][0]
                if (best is None) or (avg < best[1]):
                    best = (start_ts, avg)
            segment = []
        else:
            segment.append(item)

    return best  # (start_ts, avg_snt)

@app.route("/")
def advisor():
    # Auth
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {TOKEN}":
        abort(401)

    # Aika (UTC)
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    cur_q15_ts = floor_to_q15(now_ts)

    # DB yhteys
    try:
        db = MySQLdb.connect(**DB)
        cur = db.cursor()
    except Exception as e:
        return text_response(f"Tietokantavirhe (yhteys): {e}", 500)

    try:
        # Menneet 7 päivää (verollinen snt/kWh)
        past_7d = fetch_past_7d_prices(cur, now_ts)
        if not past_7d:
            return text_response("Viimeisen 7 päivän hintatietoa ei ole saatavilla.", 503)
        past_7d_avg = sum(past_7d) / Decimal(len(past_7d))

        # Tulevat hinnat (alkaen nykyisestä vartista), horisontti dynaaminen
        future_horizon_q = compute_future_horizon_q(now, cur_q15_ts)
        future = fetch_q15_prices_since(cur, cur_q15_ts, limit=future_horizon_q)

        if not future or len(future) < WINDOW_Q:
            return text_response("Tulevia varttihintoja ei ole riittävästi.", 503)

        # Nykyinen 3h-ikkuna (alkaa tästä vartista)
        if len(future) >= WINDOW_Q:
            current_window_prices = [p for _, p in future[:WINDOW_Q]]
            current_avg = sum(current_window_prices) / Decimal(WINDOW_Q)
        else:
            current_avg = None

        # Minimi seuraavan 12 tunnin sisällä (48 varttia)
        within_12h = future[:48] if len(future) >= 48 else future[:]
        best_12h = best_q15_window(within_12h, WINDOW_Q)  # (start_ts, avg) tai None

        # Minimi kaikesta saatavilla olevasta tulevasta
        best_all = best_q15_window(future, WINDOW_Q)

    except Exception as e:
        try:
            db.close()
        except Exception:
            pass
        return text_response(f"Tietokantavirhe (haku/laskenta): {e}", 500)
    finally:
        try:
            db.close()
        except Exception:
            pass

    # Viestirunko
    msg = []
    msg.append(
        "Sähkön keskihinta viimeisen seitsemän päivän ajalta on "
        f"{round_snt(past_7d_avg):.1f}".replace(".", ",")
        + " senttiä."
    )

    # Jos nykyinen 3h on selvästi keskiarvoa halvempi
    if current_avg is not None:
        if current_avg < past_7d_avg * Decimal("0.90"):
            msg.append(
                "Seuraava kolmen tunnin jakso on halpa, vain "
                f"{round_snt(current_avg):.1f}".replace(".", ",")
                + " senttiä."
            )

    # 12h sisällä paras
    if best_12h:
        start12, avg12 = best_12h
        # Suositusteksti, jos 12h-minimi on halvempi kuin nykyinen 3h
        if (current_avg is None) or (avg12 < current_avg):
            msg.append(
                "Seuraavan 12 tunnin sisällä halvimmillaan hinta on "
                f"{round_snt(avg12):.1f}".replace(".", ",")
                + f" senttiä alkaen {human_time(start12)}."
            )

    # Kaikesta tulevasta paras
    if best_all:
        start_all, avg_all = best_all
        # Jos koko horisontin minimi on vielä 12h-minimiäkin halvempi, mainitse se
        if (best_12h is None) or (avg_all < best_12h[1]):
            msg.append(
                "Laajemmalla tarkastelulla halvimmillaan hinta on "
                f"{round_snt(avg_all):.1f}".replace(".", ",")
                + f" senttiä alkaen {human_time(start_all)}."
            )

    # Lopullinen suositus: valitaan paras (pienin keskiarvo)
    candidates = []
    if current_avg is not None:
        candidates.append((cur_q15_ts, current_avg, "current"))
    if best_12h:
        candidates.append((best_12h[0], best_12h[1], "12h"))
    if best_all:
        candidates.append((best_all[0], best_all[1], "future"))

    if not candidates:
        return text_response("Suositusta ei voitu muodostaa.", 503)

    best_start, best_avg, best_label = min(candidates, key=lambda x: x[1])
    best_human = human_time(best_start)

    # Päätöslause: onko selvästi kallista vai kannattaako aloittaa
    if best_avg >= past_7d_avg * Decimal("0.90"):
        msg.append("Sähkö on melko kallista lähiaikoina, pesua ei ehkä kannata aloittaa vielä.")
    else:
        if best_label == "current":
            msg.append("Paras hetki on käytännössä nyt – pesu kannattaa aloittaa heti.")
        else:
            msg.append(f"Paras hetki pesulle on {best_human}.")

    return text_response(" ".join(msg))

if __name__ == "__main__":
    # Portti sama kuin ennen, vaihda tarvittaessa
    app.run(port=5000)

