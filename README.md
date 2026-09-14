# Job Posting Scraper

Automatically checks Greenhouse, Lever, and Workday job boards for internship / entry-level
postings, logs new ones to `data/postings.csv` and a seperate Google Sheet; notifies
and email using Gmail. Slack notifications can be added if it were to be set up. 
Runs daily at certain time intervals via GitHub Actions.

## Google Sheet link: 

`https://docs.google.com/spreadsheets/d/1xZ2o_mvW5pfAB1ui81F3Peh7Jq9WsA67FbrnrJysDXs/edit?usp=sharing`

## Targeted Companies

Edit `companies.json` to add, remove, or enable/disable tracked companies. Keyword settings located in `scraper.py`.

## Company(s) config

Intial screening process before Gemini evaluation

- `companies.json` — enabled companies and their ATS-specific configuration. Greenhouse and Lever entries need a `slug`; Workday entries need `tenant`, `wd_server`, and `site`.
- `ROLE_KEYWORDS` — entry-level & internship termininology screened/required before a job is considered.
- `INTEREST_KEYWORDS` — engineering specialties and their match-score weights.
- `DESCRIPTION_KEYWORDS` — lower-weight technical signals found in job descriptions.
- `EXCLUDE_KEYWORDS` — words that disqualify a match (filters out senior roles, etc).
- `MIN_MATCH_SCORE` — minimum score required to send an alert. A generic intern role
  is intentionally not enough; an `Electrical Engineering Intern` is.

Job descriptions supplement, but never replace, the title requirement for an
internship or entry-level role. Each new posting includes a title score, a
description score, and match reasons so the ranking is easy to tune.

**Finding company slugs:** search "[company name] careers greenhouse" or
"[company name] careers lever" — most tech companies use one of these two
platforms. Refer to `Limitations` section.

## Notifications

Uses Gmail's SMTP for notification on when the scraper is ran, and what job's were successfully identified.

- `SMTP_GMAIL_USER` — Gmail Address
- `SMTP_GMAIL_PASS` — Gmail **App Password** (requires 2FA enabled)
- `NOTIFY_GMAIL_EMAIL` — Recipient email address

## Gemini job evaluation (optional)

- `GEMINI_API_KEY` — the primary Gemini API key.
- `GEMINI_API_KEY_SECONDARY` — an optional key from a separate Google Cloud
  project. When the primary project's model routes are rate-limited or
  temporarily unavailable, the scraper automatically tries this project's
  models. Gemini rate limits apply per project.

## Automation

The workflow is scheduled to run daily at 0, 12, 14, 16, 18, 20, & 22 UTC, Mon - Fri via the
`cron` line in `.github/workflows/job-scraper.yml`. Times were respectively chosen to match the daily working hours in west coast. 
Each run:

1. Fetches current postings from configured companies
2. Filters for configured keywords
3. Compares against previously logged postings
4. Logs anything new to `data/postings.csv` and to a Google Sheet; commits it back to the repo
5. Sends a email notification if new postings were found

## Spreadsheets

- `data/postings.csv` opens directly in Excel, Google Sheets, or Numbers. Can also just the .csv file in `data/`.
- `GOOGLE_SHEET_ID` — Sheet Id
- `GOOGLE_SHEETS_CREDENTIALS` — Credentials for Google Sheets API. Service Account is created to update Job Postings file.

## Limitations

- Greenhouse, Lever, and Workday are supported. Other ATS platforms and custom
  career sites need an additional fetcher.
- If a company uses iCIMS or a custom site instead, the 
script won't cover them (those don't expose a simple public API). It would require 
a separate scraper tailored to that site's HTML structure.
- Does not cover LinkedIn, Indeed, or Handshake.
- GitHub Actions free tier includes 2,000 minutes/month for private repos.
