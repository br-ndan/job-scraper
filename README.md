# Job Posting Scraper

Automatically checks Greenhouse, Lever, and Workday job boards for internship / entry-level
postings, logs new ones to `data/postings.csv`, and notifies you via Slack
and/or email. Runs daily via GitHub Actions — no server required.

## Step 1: Create the GitHub repo

1. Go to https://github.com/new and create a new **private** repository
   (private keeps your target company list and postings log to yourself).
2. Upload these files to it, preserving the folder structure:
   ```
   job-scraper/
   ├── .github/workflows/job-scraper.yml
   ├── data/.gitkeep
   ├── scraper.py
   ├── requirements.txt
   └── README.md
   ```
   Easiest way: on your computer, `git init`, `git add .`, `git commit -m "initial commit"`,
   then push to the new repo. Or use GitHub's "upload files" button in the web UI.

## Step 2: Customize your search

Edit `companies.json` to add, remove, or enable/disable tracked companies. Then edit the keyword settings in `scraper.py`:

- `companies.json` — enabled companies and their ATS-specific configuration. Greenhouse and Lever entries need a `slug`; Workday entries need `tenant`, `wd_server`, and `site`.
- `ROLE_KEYWORDS` — entry-level role terms required before a job is considered.
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
platforms. If a company uses Workday, iCIMS, or a custom site instead, this
script won't cover them out of the box (those don't expose a simple public API) —
you'd want a separate scraper tailored to that site's HTML structure, and I'm happy
to help write one when you have specific companies in mind.

## Step 3: Set up notifications (optional but recommended)

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**
and add any of these:

**For Slack:**
- `SLACK_WEBHOOK_URL` — create one at https://api.slack.com/messaging/webhooks
  (Slack app → Incoming Webhooks → Add to a channel)

**For email (using Gmail as an example):**
- `SMTP_GMAIL_USER` — your Gmail address
- `SMTP_GMAIL_PASS` — a Gmail **App Password** (not your regular password — generate one at
  https://myaccount.google.com/apppasswords, requires 2FA enabled)
- `NOTIFY_GMAIL_EMAIL` — the address you want notifications sent to (can be the same as SMTP_GMAIL_USER)

You can set up just one of these, both, or neither (the postings will still get
logged to the spreadsheet either way, you just won't get pinged).

<<<<<<< HEAD
**For Gemini job evaluation (optional):**
- `GEMINI_API_KEY` — the primary Gemini API key.
- `GEMINI_API_KEY_SECONDARY` — an optional key from a separate Google Cloud
  project. When the primary project's model routes are rate-limited or
  temporarily unavailable, the scraper automatically tries this project's
  models. Do not use a second key from the same project: Gemini rate limits
  apply per project.

=======
>>>>>>> 2c42fdb62eebdebd2ab0db5389e75532f4669810
## Step 4: Test it manually

1. Go to your repo's **Actions** tab.
2. Click "Job Scraper" in the left sidebar.
3. Click **Run workflow** → **Run workflow** to trigger it manually.
4. Check the run logs to confirm it worked, then check `data/postings.csv`
   in your repo for logged results.

## Step 5: Let it run automatically

That's it — the workflow is scheduled to run daily at 13:00 UTC via the
`cron` line in `.github/workflows/job-scraper.yml`. Edit that line if you want
a different time. Each run:

1. Fetches current postings from your configured companies
2. Filters for your keywords
3. Compares against previously logged postings
4. Logs anything new to `data/postings.csv` and commits it back to the repo
5. Sends a Slack/email notification if new postings were found

## Viewing your spreadsheet

`data/postings.csv` opens directly in Excel, Google Sheets, or Numbers.
Since GitHub Actions commits updates automatically, you can also just view
the file on GitHub.com to see the running log without downloading anything.

## Limitations

- Greenhouse, Lever, and Workday are supported. Other ATS platforms and custom
  career sites need an additional fetcher.
- Does not cover LinkedIn, Indeed, or Handshake — those block automated
  scraping. Use their native "Job Alerts" features for those platforms instead.
- GitHub Actions free tier includes 2,000 minutes/month for private repos,
  which is far more than this lightweight daily job needs.
