"""Unicorn beach day forecast for China Beach, San Francisco.

Estimates, for each of the next 7 days, the chance of a "unicorn" window:
3+ consecutive daylight hours with waves of 1 ft or less off China Beach,
wind of 5.6 mph or less and gusts of 8.9 mph or less. Historically (2021–2025)
that happened about twice a year.

Writes site/data.json for the web page and sends an email alert when a unicorn
day looks likely within 48 hours. Standard library only.

Email alerts need these environment variables (GitHub Actions secrets):
  GMAIL_USER (the sending Gmail address), GMAIL_APP_PASSWORD (a Google app password),
  ALERT_EMAILS (comma-separated; everyone is sent the alert in BCC).
Without them it prints the message instead of sending.
"""
import json, math, os, re, smtplib, sys, time, urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
PT = ZoneInfo('America/Los_Angeles')
LAT, LON = 37.789, -122.494          # China Beach
UA = {'User-Agent': '(unicorn-beach alert, personal project)'}

# The unicorn definition, from 5 years of history (see README).
WAVE_MAX_M = 0.30      # 1.0 ft, modeled significant height ~10 m deep off China Beach
WIND_MAX_MPH = 5.6
GUST_MAX_MPH = 8.9
HOURS_NEEDED = 3
CHINA_TO_OFFSHORE = 0.37  # median China/Bar-buoy wave ratio, 2021–2025
ALERT_CHANCE = 0.5        # text when a day within 48 h reaches this chance
BLOCKING_ALERTS = ('Small Craft', 'High Surf', 'Gale', 'Storm', 'Beach Hazards', 'Hazardous Seas', 'Tsunami')

CDIP = 'https://thredds.cdip.ucsd.edu/thredds/dodsC/cdip/model/MOP_grids/SF_0.002_seaswellfc.nc.ascii'
CDIP_CELL = (95, 102)     # grid indices of the cell off China Beach (37.790, -122.494, ~11 m deep)
NWS_GRID = 'https://api.weather.gov/gridpoints/MTR/82,106'
MARINE = ('https://marine-api.open-meteo.com/v1/marine?latitude=37.78&longitude=-122.63'
          '&hourly=wave_height&timezone=UTC&forecast_days=8')


def get(url, headers=UA, tries=4):
    for i in range(tries):  # data servers (CDIP, NWS) refuse or time out now and then; wait and retry
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(15 * (i + 1))


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def hour_floor(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


# ---------- data sources ----------

def cdip_china():
    """China Beach cell wave forecast: {utc hour: Hs m}, 6-hourly, ~3.5 days."""
    i, j = CDIP_CELL
    q = f'waveTime,waveHs%5B0:1:15%5D%5B{i}%5D%5B{j}%5D,waveFlagPrimary%5B0:1:15%5D%5B{i}%5D%5B{j}%5D'
    text = get(f'{CDIP}?{q}')
    arr = {}
    for block in text.split('---------------------------------------------\n')[1].strip().split('\n\n'):
        lines = block.strip().splitlines()
        name = lines[0].split('[')[0].split('.')[-1]
        if lines[1].startswith('['):
            arr[name] = [float(x.split(',', 1)[1]) for x in lines[1:]]
        else:
            arr[name] = [float(x) for x in ','.join(lines[1:]).split(',')]
    out = {}
    for t, hs, qc in zip(arr['waveTime'], arr['waveHs'], arr['waveFlagPrimary']):
        if qc == 1:
            out[datetime.fromtimestamp(t, timezone.utc)] = hs
    return out


def iso_duration_hours(d):
    m = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?)?', d)
    return int(m.group(1) or 0) * 24 + int(m.group(2) or 0)


def nws_series(props, key, conv):
    """Expand an NWS gridpoint layer into {utc hour: value}."""
    out = {}
    for v in props.get(key, {}).get('values', []):
        if v['value'] is None:
            continue
        start, dur = v['validTime'].split('/')
        t0 = datetime.fromisoformat(start)
        for h in range(max(1, iso_duration_hours(dur))):
            out[t0 + timedelta(hours=h)] = conv(v['value'])
    return out


def nws_weather():
    props = json.loads(get(NWS_GRID))['properties']
    kmh = lambda x: x * 0.621371
    return (nws_series(props, 'windSpeed', kmh), nws_series(props, 'windGust', kmh),
            nws_series(props, 'probabilityOfPrecipitation', float), props.get('updateTime'))


def offshore_waves():
    d = json.loads(get(MARINE))['hourly']
    return {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): h
            for t, h in zip(d['time'], d['wave_height']) if h is not None}


def tide_predictions(start_local, end_local):
    """NOAA high/low tide predictions at Fort Point (Golden Gate), feet above MLLW, local time."""
    url = ('https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=predictions&station=9414290'
           f'&begin_date={start_local:%Y%m%d}&end_date={end_local:%Y%m%d}&datum=MLLW&interval=hilo'
           '&units=english&time_zone=lst_ldt&format=json&application=unicorn_beach')
    try:
        preds = json.loads(get(url))['predictions']
    except Exception as e:  # tides are a bonus; never block the forecast
        print('tides unavailable:', e, file=sys.stderr)
        return {}
    out = {}
    for p in preds:
        t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
        if t < sunrise_local(t.date()):  # skip tides between midnight and sunrise
            continue
        out.setdefault(t.date().isoformat(), []).append(dict(
            time=t.strftime('%-I:%M%p').lower().replace(':00', ''), type='high' if p['type'] == 'H' else 'low',
            ft=round(float(p['v']), 1)))
    return out


def tide_curves(start_local, end_local):
    """Hourly predicted water level at Fort Point: {date: [[hour, ft], ...]} from sunrise to midnight."""
    url = ('https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=predictions&station=9414290'
           f'&begin_date={start_local:%Y%m%d}&end_date={(end_local + timedelta(days=1)):%Y%m%d}&datum=MLLW&interval=h'
           '&units=english&time_zone=lst_ldt&format=json&application=unicorn_beach')
    try:
        preds = json.loads(get(url))['predictions']
    except Exception as e:
        print('tide curve unavailable:', e, file=sys.stderr)
        return {}
    by_time = {datetime.strptime(p['t'], '%Y-%m-%d %H:%M'): float(p['v']) for p in preds}
    out = {}
    d = start_local
    while d <= end_local:
        rise = sunrise_local(d)
        pts = []
        for t in sorted(by_time):
            if t.date() == d and t >= rise.replace(minute=0):
                pts.append([t.hour, round(by_time[t], 2)])
            elif t == datetime(d.year, d.month, d.day) + timedelta(days=1):
                pts.append([24, round(by_time[t], 2)])  # midnight closes the curve
        if pts:
            out[d.isoformat()] = dict(points=pts, sunrise=round(rise.hour + rise.minute / 60, 2))
        d += timedelta(days=1)
    return out


def active_alerts():
    try:
        feats = json.loads(get(f'https://api.weather.gov/alerts/active?point={LAT},{LON}'))['features']
    except Exception as e:  # an outage here shouldn't stop the forecast
        print('alerts unavailable:', e, file=sys.stderr)
        return []
    out = []
    for f in feats:
        p = f['properties']
        if any(k in p['event'] for k in BLOCKING_ALERTS):
            out.append(dict(event=p['event'], start=p.get('onset') or p.get('effective'),
                            end=p.get('ends') or p.get('expires')))
    return out


# ---------- the model ----------

def sun_up(dt_utc, min_elev=3):
    doy = dt_utc.timetuple().tm_yday
    g = 2 * math.pi / 365 * (doy - 1 + (dt_utc.hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    ha = math.radians((dt_utc.hour * 60 + dt_utc.minute + eqt + 4 * LON) / 4 - 180)
    lat = math.radians(LAT)
    elev = math.asin(math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha))
    return math.degrees(elev) >= min_elev


def sunrise_local(day):
    """Local sunrise (sun at the horizon) for a date, to the nearest 5 minutes."""
    t = datetime(day.year, day.month, day.day, 4, tzinfo=PT)
    while t.hour < 10:
        if sun_up(t.astimezone(timezone.utc), min_elev=-0.8):
            return t.replace(tzinfo=None)
        t += timedelta(minutes=5)
    return datetime(day.year, day.month, day.day, 7)


def interp(series, t):
    """Linear interpolation inside a sparse {datetime: value} series; None outside it."""
    times = sorted(series)
    if not times or t < times[0] or t > times[-1]:
        return None
    for a, b in zip(times, times[1:]):
        if a <= t <= b:
            f = (t - a) / (b - a) if b > a else 0
            return series[a] + f * (series[b] - series[a])
    return series[times[-1]]


def build(now=None):
    now = now or datetime.now(timezone.utc)
    cdip = cdip_china()
    wind, gust, pop, nws_updated = nws_weather()
    offshore = offshore_waves()
    alerts = active_alerts()

    hours = []
    t = hour_floor(now) + timedelta(hours=1)
    # exactly 7 calendar days: today through day 7, ending at local midnight (not 7x24 h from now)
    today = now.astimezone(PT).date()
    end = datetime(today.year, today.month, today.day, tzinfo=PT) + timedelta(days=7)
    while t < end:
        mid = t + timedelta(minutes=30)
        if sun_up(mid) and t in wind:
            lead = (t - now).total_seconds() / 86400
            hs = interp(cdip, mid)
            if hs is not None:
                src, sig_h = 'cdip', 0.04 + 0.015 * lead
            elif t in offshore:
                hs, src, sig_h = offshore[t] * CHINA_TO_OFFSHORE, 'offshore', 0.07 + 0.015 * lead
            else:
                t += timedelta(hours=1)
                continue
            w = wind[t]
            g = gust.get(t, w * 1.6)
            rain = pop.get(t, 0) / 100
            p = (phi((WAVE_MAX_M - hs) / sig_h)
                 * phi((WIND_MAX_MPH - w) / (1.5 + 0.4 * lead))
                 * phi((GUST_MAX_MPH - g) / (2.0 + 0.5 * lead))
                 * (1 - rain))
            blocked = [a['event'] for a in alerts
                       if a['start'] and a['end'] and datetime.fromisoformat(a['start']) <= t <= datetime.fromisoformat(a['end'])]
            if blocked:
                p *= 0.2
            hours.append(dict(t=t, p=p, hs=hs, src=src, wind=w, gust=g, rain=rain, blocked=blocked))
        t += timedelta(hours=1)

    by_day = {}
    for h in hours:
        by_day.setdefault(h['t'].astimezone(PT).date(), []).append(h)

    days = []
    for d in sorted(by_day):
        hs_ = by_day[d]
        best = None
        for k in range(len(hs_) - HOURS_NEEDED + 1):
            run = hs_[k:k + HOURS_NEEDED]
            if (run[-1]['t'] - run[0]['t']) != timedelta(hours=HOURS_NEEDED - 1):
                continue
            p = min(r['p'] for r in run)
            # When every chance rounds to zero, show the window closest to unicorn conditions.
            excess = sum(max(r['hs'] / WAVE_MAX_M, r['wind'] / WIND_MAX_MPH, r['gust'] / GUST_MAX_MPH) for r in run)
            key = (round(p, 4), -excess)
            if best is None or key > best[0]:
                best = (key, p, run)
        if not best:
            continue
        _, p, run = best
        days.append(dict(
            date=d.isoformat(), label=d.strftime('%a %b %-d'), chance=round(p, 3),
            window=f"{run[0]['t'].astimezone(PT).strftime('%-I %p')}–{(run[-1]['t'] + timedelta(hours=1)).astimezone(PT).strftime('%-I %p')}".replace(' AM', 'am').replace(' PM', 'pm'),
            wave_ft=round(max(r['hs'] for r in run) * 3.281, 1),
            wind_mph=round(max(r['wind'] for r in run)),
            gust_mph=round(max(r['gust'] for r in run)),
            rain_pct=round(max(r['rain'] for r in run) * 100),
            wave_source=run[0]['src'],
            advisory=sorted({b for r in run for b in r['blocked']}),
            within_48h=(run[0]['t'] - now) <= timedelta(hours=48),
        ))
    tides = tide_predictions(now.astimezone(PT).date(), today + timedelta(days=6))
    curves = tide_curves(now.astimezone(PT).date(), today + timedelta(days=6))
    for d in days:
        d['tides'] = tides.get(d['date'], [])
        c = curves.get(d['date'])
        if c:
            d['tide_curve'] = c['points']
            d['sunrise'] = c['sunrise']
        hts = [t['ft'] for t in d['tides']]
        highs = [t['ft'] for t in d['tides'] if t['type'] == 'high']
        lows = [t['ft'] for t in d['tides'] if t['type'] == 'low']
        if highs and lows:
            d['tide_range'] = dict(ft=round(max(highs) - min(lows), 1), high=max(highs), low=min(lows))
        elif d.get('tide_curve'):  # only highs or only lows after sunrise: use the curve's own swing
            v = [p[1] for p in d['tide_curve']]
            d['tide_range'] = dict(ft=round(max(v) - min(v), 1), high=round(max(v), 1), low=round(min(v), 1))
    return dict(
        generated=now.isoformat(), generated_local=now.astimezone(PT).strftime('%a %b %-d, %-I:%M %p'),
        definition=dict(wave_max_ft=round(WAVE_MAX_M * 3.281, 1), wind_max_mph=WIND_MAX_MPH,
                        gust_max_mph=GUST_MAX_MPH, hours=HOURS_NEEDED, per_year=2),
        days=days, alerts=alerts,
        sources=dict(cdip_hours=len(cdip), nws_updated=nws_updated),
    )


# ---------- alerts ----------

def send_emails(subject, body):
    user, password = os.environ.get('GMAIL_USER'), os.environ.get('GMAIL_APP_PASSWORD')
    recipients = [e.strip() for e in os.environ.get('ALERT_EMAILS', '').split(',') if e.strip()]
    if not (user and password and recipients):
        print('[dry run, no Gmail settings] would email:', subject, '|', body)
        return False
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = subject, f'Unicorn Beach Day <{user}>', user
    msg['Bcc'] = ', '.join(recipients)
    msg.set_content(body)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f'emailed {len(recipients)} address(es)')
    return True


def main():
    data = build()
    site = ROOT / 'site'
    (site / 'data.json').write_text(json.dumps(data, indent=1))
    state_path = ROOT / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'alerted': []}

    page = os.environ.get('PAGE_URL', '')
    for d in data['days']:
        if d['within_48h'] and d['chance'] >= ALERT_CHANCE and d['date'] not in state['alerted']:
            subject = f"Drop everything and go to the beach {d['label']}! China Beach is glassy"
            body = (f"Skip work. Grab your towel. China Beach looks like a unicorn day {d['label']}, {d['window']}.\n\n"
                    f"Waves about {d['wave_ft']} ft, wind {d['wind_mph']} mph, gusts {d['gust_mph']} mph. "
                    f"Chance {round(d['chance'] * 100)}%.\n\n"
                    f"This happens about twice a year. Drop everything and get out there.\n\n"
                    f"{page}\n\n"
                    "Check currents and shorebreak on the beach yourself; this is a forecast, not a safety call.\n"
                    "Reply 'stop' to unsubscribe.")
            if send_emails(subject, body) or os.environ.get('MARK_DRY_RUN'):
                state['alerted'].append(d['date'])
    state['alerted'] = sorted(set(state['alerted']))[-50:]
    state_path.write_text(json.dumps(state, indent=1))

    for d in data['days']:
        print(f"{d['label']:<11} {d['chance'] * 100:5.1f}%  {d['window']:<10} waves {d['wave_ft']} ft ({d['wave_source']}), "
              f"wind {d['wind_mph']} gust {d['gust_mph']} mph, rain {d['rain_pct']}% {', '.join(d['advisory'])}")


if __name__ == '__main__':
    main()
