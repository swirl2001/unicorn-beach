# Unicorn Beach Day

A web page and text alert for the rare perfect day at China Beach, San Francisco:
3+ daylight hours with waves of 1 ft or less, wind of 5.6 mph or less and gusts of 8.9 mph or less.
From 2021 to 2025 that happened about twice a year.

## How it works

`forecast.py` runs twice a day on GitHub Actions (about 5 pm and 5 am Pacific). It:

1. Pulls the CDIP wave forecast for the grid cell off China Beach (about 3.5 days), the
   National Weather Service hourly wind, gust and rain forecast, and marine advisories.
2. For days 4–7, estimates China Beach waves as 0.37 × the Open-Meteo offshore forecast
   (the median ratio of China Beach to the Bar buoy, 2021–2025).
3. Scores each daylight hour, finds each day's best 3-hour window, and writes `site/data.json`.
4. Texts everyone in `ALERT_NUMBERS` once per date when a day within 48 hours reaches a 50% chance.

The web page in `site/` reads `data.json` and is published with GitHub Pages.

The chances are a forecast-based estimate, not a calibrated probability: archived forecasts
weren't available to test against. Expect them to be cautious.

## Setup

GitHub repository secrets:

| Secret | Value |
| --- | --- |
| `TWILIO_ACCOUNT_SID` | From the Twilio console |
| `TWILIO_AUTH_TOKEN` | From the Twilio console |
| `TWILIO_FROM` | Your Twilio number, e.g. `+18885550123` |
| `ALERT_NUMBERS` | Comma-separated numbers in `+1…` format |

Then enable GitHub Pages with "GitHub Actions" as the source, and run the workflow once by hand.

Run locally: `python3 forecast.py` (prints the message instead of texting when secrets are missing).

## Background research

The historic analysis that set these limits lives in `~/Documents/ChatGPT/Ocean Data/`
(`find_dreamy_days.py`, `outputs/dreamy_days_report.md`).

Data: CDIP, Scripps Institution of Oceanography; NOAA National Weather Service and NDBC; Open-Meteo.
