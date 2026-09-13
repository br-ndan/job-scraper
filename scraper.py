"""
Job Posting Scraper
Checks company job boards (Greenhouse + Lever) for internship / entry-level
postings, logs new ones to data/postings.csv, and sends a notification
(email and/or Slack) when new matches are found.
 
Configure the companies you want to track and your keyword filters below.
"""
 
import csv
import json
import os
import re
import smtplib
import time
from email.mime.text import MIMEText
from html import unescape
from pathlib import Path
import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
 
# ---------------------------------------------------------------------------
# CONFIG — edit this section to customize your search
# 
# Version 3 = Gemini API integration for AI evaluation.
# Version 3.1 = A Gemini 503 UNAVAILABLE now temporarily removes that model 
# from rotation and immediately tries the next configured model, just like a 
# 429 rate-limit response. It also handles transient 500/502/504 errors.
# Also added Gemini 3.6 Flash model.
# Version 3.2 = Secondary API key's models are utilized when primary routes are busy
# ---------------------------------------------------------------------------

COMPANIES_FILE = Path(__file__).with_name("companies.json")
SUPPORTED_PLATFORMS = {"greenhouse", "lever", "workday"}


def load_companies(companies_file=COMPANIES_FILE):
    """Load and validate ATS company definitions from a JSON file."""
    try:
        with companies_file.open(encoding="utf-8") as file:
            companies = json.load(file)
    except FileNotFoundError as error:
        raise ValueError(f"Company configuration file not found: {companies_file}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in company configuration: {error}") from error

    if not isinstance(companies, list):
        raise ValueError("Company configuration must be a JSON array.")

    required_by_platform = {
        "greenhouse": ("slug",),
        "lever": ("slug",),
        "workday": ("tenant", "wd_server", "site"),
    }
    validated = []
    seen_companies = set()
    for index, company in enumerate(companies, start=1):
        prefix = f"Company entry {index}"
        if not isinstance(company, dict):
            raise ValueError(f"{prefix} must be an object.")
        if not isinstance(company.get("name"), str) or not company["name"].strip():
            raise ValueError(f"{prefix} must include a non-empty name.")
        if company.get("platform") not in SUPPORTED_PLATFORMS:
            raise ValueError(f"{prefix} has unsupported platform {company.get('platform')!r}.")
        if not isinstance(company.get("enabled", True), bool):
            raise ValueError(f"{prefix} enabled must be true or false.")

        normalized = {**company, "name": company["name"].strip(), "enabled": company.get("enabled", True)}
        for field in required_by_platform[normalized["platform"]]:
            if not isinstance(normalized.get(field), str) or not normalized[field].strip():
                raise ValueError(f"{prefix} ({normalized['name']}) must include a non-empty {field}.")
            normalized[field] = normalized[field].strip()

        key = (normalized["platform"], normalized["name"].casefold())
        if key in seen_companies:
            raise ValueError(f"Duplicate company entry for {normalized['name']} on {normalized['platform']}.")
        seen_companies.add(key)
        validated.append(normalized)
    return validated


COMPANIES = load_companies()


# ---------------------------------------------------------------------------
# Company scoring — edit this section to customize scoring
# 
# ---------------------------------------------------------------------------


# A role-level keyword is required before a job can be considered. Scores then
# distinguish a relevant engineering opportunity from a generic internship.
ROLE_KEYWORDS = {
    "internship": 10,
    "intern": 10,
    "entry level": 10,
    "entry-level": 10,
    "new grad": 10,
    "graduate": 5,
    "junior": 5,
}

# Adjust these weights as your search evolves. Phrases are scored only once,
# even when a title contains the same phrase more than once.
INTEREST_KEYWORDS = {
    "electrical": 20,
    "power systems": 25,
    "power electronics": 25,
    "protection": 25,
    "protection and control": 25,
    "relay": 25,
    "substation": 25,
    "switchgear": 25,
    "hardware": 15,
    "embedded": 15,
    "semiconductor": 15,
    "data center": 15,
    "data center electrical": 25,
    "critical facilities": 20,
    "electrical infrastructure": 25,
    "utility": 15,
    "utilities": 15,
    "defense": 15,
}

# Description signals are deliberately weaker than title signals. A job must
# still have an entry-level title, while the description supplies engineering
# context that titles often omit.
DESCRIPTION_KEYWORDS = {
    "electrical": 8,
    "power systems": 12,
    "power electronics": 12,
    "protection": 10,
    "protection and control": 12,
    "relay": 12,
    "substation": 12,
    "switchgear": 12,
    "hardware": 8,
    "pcb": 8,
    "embedded": 8,
    "semiconductor": 8,
    "data center electrical": 12,
    "critical facilities": 10,
    "electrical infrastructure": 12,
    "transmission": 10,
    "distribution": 10,
    "scada": 10,
    "defense": 8,
}
MAX_DESCRIPTION_SCORE = 30

# A generic "Intern" scores 10 and is ignored. "Electrical Intern" scores
# 30 and is included. Tune this threshold without changing filtering logic.
MIN_MATCH_SCORE = 25
 
# Keywords that disqualify a match even if a title keyword matched
# (helps filter out "Senior Engineer - mentors interns" type false positives)
EXCLUDE_KEYWORDS = [
    "senior",
    "staff",
    "principal",
    "director",
    "lead",
    #"hr",      -- risky, some words may contain the "hr" string (rare). only include for hard blacklist
    "human resources",
    "recruiting",
    "recruiter",
    "talent acquisition",
    "marketing",
    "communications",
    "sales",
    "finance",
    "accounting",
    "legal",
    "supply chain",
    "human resource",
    "technician",
]
 
DATA_FILE = Path(__file__).parent / "data" / "postings.csv"
MAX_REQUEST_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Gemini model routing.  Lite models are preferred because their higher RPM
# limit makes them the best default for the large number of small evaluations
# this scraper performs.  Keep these names aligned with the models enabled for
# your Google AI Studio project.
GEMINI_MODEL_LIMITS = (
    ("gemini-3.5-flash-lite", 15),
    ("gemini-3.1-flash-lite", 15),
    ("gemini-3.5-flash", 5),
    ("gemini-3.6-flash", 5),
    ("gemini-3.7-flash", 5),
    ("gemini-3.8-flash", 5),
)
DEFAULT_GEMINI_RATE_LIMIT_COOLDOWN_SECONDS = 60
POSTING_FIELDS = [
    "job_id",
    "company",
    "source",
    "title",
    "location",
    "url",
    "date_found",
    "match_score",
    "title_score",
    "description_score",
    "match_reasons",
]
 
# ---------------------------------------------------------------------------
# SCRAPING LOGIC - Greenhouse + Lever Jobs
# ---------------------------------------------------------------------------


def request_with_retries(method, url, *, max_attempts=MAX_REQUEST_ATTEMPTS, backoff_seconds=1, **kwargs):
    """Send an HTTP request, retrying only transient failures with backoff."""
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException:
            if attempt == max_attempts:
                raise
            delay = backoff_seconds * (2 ** (attempt - 1))
            print(f"  [!] Request failed; retrying in {delay}s (attempt {attempt + 1}/{max_attempts}).")
            time.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES or attempt == max_attempts:
            return response

        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else backoff_seconds * (2 ** (attempt - 1))
        except ValueError:
            delay = backoff_seconds * (2 ** (attempt - 1))
        print(f"  [!] Received HTTP {response.status_code}; retrying in {delay}s (attempt {attempt + 1}/{max_attempts}).")
        time.sleep(delay)


def fetch_greenhouse_jobs(company):     # replaced company_slug with company
    """Fetch all jobs for a company from Greenhouse's public API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs"     # replaced {company_slug} to {company['slug']}
    try:
        resp = request_with_retries("get", url, params={"content": "true"}, timeout=15)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
    except requests.RequestException as e:
        print(f"  [!] Failed to fetch Greenhouse jobs for {company['slug']}: {e}")
        return []
 
    results = []
    for job in jobs:
        results.append(
            {
                "company": company["name"], # changed company_slug to company["name"]
                "source": "greenhouse",
                "title": job.get("title", ""),
                "location": (job.get("location") or {}).get("name", ""),
                "url": job.get("absolute_url", ""),
                "job_id": str(job.get("id", "")),
                "description": html_to_text(job.get("content", "")),
            }
        )
    return results
 
 
def fetch_lever_jobs(company):     # repeated changes done to greenhouse companies
    """Fetch all jobs for a company from Lever's public API."""
    url = f"https://api.lever.co/v0/postings/{company['slug']}?mode=json"
    try:
        resp = request_with_retries("get", url, timeout=15)
        resp.raise_for_status()
        jobs = resp.json()
    except requests.RequestException as e:
        print(f"  [!] Failed to fetch Lever jobs for {company['slug']}: {e}")
        return []
 
    results = []
    for job in jobs:
        results.append(
            {
                "company": company["name"],
                "source": "lever",
                "title": job.get("text", ""),
                "location": (job.get("categories") or {}).get("location", ""),
                "url": job.get("hostedUrl", ""),
                "job_id": str(job.get("id", "")),
                "description": job.get("descriptionPlain") or html_to_text(job.get("description", "")),
            }
        )
    return results

# ---------------------------------------------------------------------------
# SCRAPING LOGIC - Workday Jobs
# ---------------------------------------------------------------------------

def fetch_workday_job_description(base_url, external_path, company_name):
    """Fetch a Workday job's full description without failing the whole run."""
    try:
        response = request_with_retries("get", f"{base_url}{external_path}", timeout=15)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"  [!] Failed to fetch Workday job details for {company_name}: {error}")
        return ""

    job_info = response.json().get("jobPostingInfo", {})
    return html_to_text(job_info.get("jobDescription", ""))


def fetch_workday_jobs(company):
    """Fetch all jobs for a company from Workday's CxS API."""
    tenant = company["tenant"]
    wd_server = company["wd_server"]
    site = company["site"]
    base_url = f"https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    results = []
    offset = 0
    limit = 20

    while True:
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        try:
            resp = request_with_retries("post", f"{base_url}/jobs", json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"  [!] Failed to fetch Workday jobs for {company['name']}: {e}")
            break

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for job in postings:
            external_path = job.get("externalPath", "")
            result = {
                "company": company["name"],
                "source": "workday",
                "title": job.get("title", ""),
                "location": job.get("locationsText", ""),
                "url": f"https://{tenant}.{wd_server}.myworkdayjobs.com/en-US/{site}{external_path}",
                #"job_id": external_path,
                "job_id": extract_req_id(external_path),
                "description": "",
            }
            if is_role_candidate(result["title"]):
                result["description"] = fetch_workday_job_description(
                    base_url, external_path, company["name"]
                )
            results.append(result)

        offset += limit
        if offset >= data.get("total", 0):
            break

    return results 

def extract_req_id(external_path):      # testing
    match = re.search(r"(R\d+(-\d+)?)$", external_path)
    return match.group(1) if match else external_path

def html_to_text(value):
    """Normalize an ATS description to searchable plain text."""
    text = re.sub(r"<[^>]+>", " ", unescape(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def is_role_candidate(title):
    """Require an entry-level role title before inspecting its description."""
    title_lower = title.casefold()
    return (
        not any(keyword in title_lower for keyword in EXCLUDE_KEYWORDS)
        and any(keyword in title_lower for keyword in ROLE_KEYWORDS)
    )


def score_keyword_matches(text, keyword_scores, source):
    """Return a score and human-readable reasons for matching configured terms."""
    text_lower = text.casefold()
    matches = []
    for keyword, score in sorted(keyword_scores.items(), key=lambda item: len(item[0]), reverse=True):
        if keyword not in text_lower:
            continue
        # A specific phrase such as "protection and control" should not also
        # receive the separate, weaker "protection" score.
        if any(keyword in selected_keyword for selected_keyword, _ in matches):
            continue
        matches.append((keyword, score))
    score = sum(score for _, score in matches)
    reasons = [f"{source}: {keyword} (+{score})" for keyword, score in matches]
    return score, reasons


def evaluate_job(title, description=""):
    """Return explainable score details, or ``None`` for ineligible roles."""
    if not is_role_candidate(title):
        return None

    title_lower = title.casefold()
    role_keyword, role_score = max(
        (
            (keyword, score)
            for keyword, score in ROLE_KEYWORDS.items()
            if keyword in title_lower
        ),
        key=lambda item: item[1],
    )
    interest_score, interest_reasons = score_keyword_matches(
        title, INTEREST_KEYWORDS, "title"
    )
    description_score, description_reasons = score_keyword_matches(
        description, DESCRIPTION_KEYWORDS, "description"
    )

    raw_description_score = description_score
    description_score = min(description_score, MAX_DESCRIPTION_SCORE)

    if raw_description_score > MAX_DESCRIPTION_SCORE:
        description_reasons.append(f"description score capped at {MAX_DESCRIPTION_SCORE}")

    title_score = role_score + interest_score
    return {
        "title_score": title_score,
        "description_score": description_score,
        "match_score": title_score + description_score,
        "match_reasons": "; ".join(
            [f"title: {role_keyword} (+{role_score})", *interest_reasons, *description_reasons]
        ),
    }


# ---------------------------------------------------------------------------
# SCRAPING LOGIC - Gemini API for Dedicated Job Boards
# ---------------------------------------------------------------------------


class GeminiModelRouter:
    """Select Gemini account/model routes while respecting each route's rate."""

    def __init__(self, model_limits=GEMINI_MODEL_LIMITS, *, api_keys=None, clock=time.monotonic, sleeper=time.sleep):
        if not model_limits or any(rpm <= 0 for _, rpm in model_limits):
            raise ValueError("Gemini model limits must contain positive RPM values.")
        self.model_limits = tuple(model_limits)
        # None preserves the original single-key behavior for callers that
        # construct a router directly (and makes focused tests simple).
        self.api_keys = tuple(api_keys) if api_keys else (None,)
        self._clock = clock
        self._sleep = sleeper
        self._next_available = {
            (api_key, model): 0.0
            for api_key in self.api_keys
            for model, _ in self.model_limits
        }

    @property
    def models(self):
        return tuple(model for model, _ in self.model_limits)

    @property
    def routes(self):
        return tuple(
            (api_key, model)
            for api_key in self.api_keys
            for model, _ in self.model_limits
        )

    def acquire(self):
        """Reserve and return a model, for backwards-compatible callers."""
        return self.acquire_route()[1]

    def acquire_route(self):
        """Reserve the highest-priority available account/model route."""
        while True:
            now = self._clock()
            for api_key in self.api_keys:
                for model, rpm in self.model_limits:
                    route = (api_key, model)
                    if self._next_available[route] <= now:
                        # Reserve the next time slot before issuing the request
                        # so consecutive evaluations are proactively paced.
                        self._next_available[route] = now + (60 / rpm)
                        return route

            earliest = min(self._next_available.values())
            delay = max(0.0, earliest - now)
            print(f"  [gemini] All account/model routes are busy; waiting {delay:.1f}s for the next slot.")
            self._sleep(delay)

    def unavailable(self, model, retry_after=None, api_key=None):
        """Temporarily remove one account/model route after a transient error."""
        route = (api_key, model)
        if route not in self._next_available:
            raise ValueError(f"Unknown Gemini model: {model}")
        cooldown = (
            retry_after
            if retry_after is not None and retry_after >= 0
            else DEFAULT_GEMINI_RATE_LIMIT_COOLDOWN_SECONDS
        )
        self._next_available[route] = max(self._next_available[route], self._clock() + cooldown)


def is_gemini_rate_limit_error(error):
    message = str(error).casefold()
    return "429" in message or "resource_exhausted" in message


def is_gemini_transient_error(error):
    """Return whether Gemini reports temporary capacity or server trouble."""
    message = str(error).casefold()
    return (
        is_gemini_rate_limit_error(error)
        or "503" in message
        or "unavailable" in message
        or "500" in message
        or "502" in message
        or "504" in message
        or "internal" in message
    )


def gemini_retry_after_seconds(error):
    """Read a retry delay when the Gemini SDK exposes one; otherwise return None."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None) or {}
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


GEMINI_ROUTER = GeminiModelRouter()
GEMINI_ROUTERS = {}


def configured_gemini_api_keys():
    """Return the primary and optional secondary Gemini API keys, de-duplicated."""
    keys = (
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("GEMINI_API_KEY_SECONDARY"),
    )
    return tuple(dict.fromkeys(key.strip() for key in keys if key and key.strip()))


def gemini_router_for_keys(api_keys):
    """Reuse route availability state throughout a scraper run."""
    if api_keys not in GEMINI_ROUTERS:
        GEMINI_ROUTERS[api_keys] = GeminiModelRouter(api_keys=api_keys)
    return GEMINI_ROUTERS[api_keys]


# Define structured output schema
class JobEvaluation(BaseModel):
    is_entry_level_or_intern: bool = Field(description="True if the role is for interns, new grads, or entry level (0-2 yrs experience). False if senior/lead/staff.")
    is_relevant_engineering: bool = Field(description="True if the role relates to electrical engineering, power systems, hardware, embedded systems, or critical infrastructure.")
    match_score: int = Field(description="Score from 0 to 100 based on fit for an electrical/power systems candidate.")
    match_reasons: str = Field(description="Short sentence explaining why it matched or failed.")

def evaluate_job_with_gemini(title: str, description: str = "", keyword_evaluation=None, max_attempts=None, router=None) -> dict:
    """Use Gemini to evaluate job relevance. Falls back to keyword scoring on any failure."""
    fallback = keyword_evaluation if keyword_evaluation is not None else evaluate_job(title, description)

    api_keys = configured_gemini_api_keys()
    if not api_keys:
        print("  [!] Missing GEMINI_API_KEY. Falling back to rule-based evaluation.")
        return fallback         # Fallback to existing keyword logic

    # Truncate long descriptions to optimize speed/cost
    prompt = f"""
    Analyze this job posting for an Electrical / Power Systems / Hardware Engineering student or new grad.

    Job Title: {title}
    Job Description: {description[:2000]}
    """

    router = router or gemini_router_for_keys(api_keys)
    # One attempt per configured model lets a rate-limit or temporary capacity
    # error immediately fail over to a different capacity pool. Callers may
    # lower this in focused tests.
    attempt_limit = len(router.routes) if max_attempts is None else max_attempts
    if attempt_limit < 1:
        return fallback

    clients = {}
    for attempt in range(1, attempt_limit + 1):
        route_api_key, model = router.acquire_route()
        # Custom routers created by older callers have a None route key; use
        # the primary configured key for that backwards-compatible route.
        api_key = route_api_key or api_keys[0]
        if api_key not in clients:
            clients[api_key] = genai.Client(api_key=api_key)
        client = clients[api_key]
        account_label = "primary" if api_key == api_keys[0] else "secondary"
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=JobEvaluation,
                    temperature=0.1,
                ),
            )
            result = JobEvaluation.model_validate_json(response.text)
            print(f"  [gemini:{account_label}:{model}] '{title[:50]}' -> score={result.match_score}, entry_level={result.is_entry_level_or_intern}, relevant={result.is_relevant_engineering}")

            # Disqualify if it's not entry level or not relevant
            if not result.is_entry_level_or_intern or not result.is_relevant_engineering:
                return None

            return {
                "title_score": result.match_score // 2,
                "description_score": result.match_score // 2,
                "match_score": result.match_score,
                "match_reasons": f"AI: {result.match_reasons}",
            }

        except Exception as e:
            if is_gemini_transient_error(e):
                delay = gemini_retry_after_seconds(e)
                router.unavailable(model, delay, route_api_key)
                cooldown = delay if delay is not None else DEFAULT_GEMINI_RATE_LIMIT_COOLDOWN_SECONDS
                if attempt < attempt_limit:
                    print(f"  [!] Gemini {account_label} model {model} temporarily unavailable; routing to another account/model route (cooldown {cooldown:g}s).")
                    continue
            print(f"  [!] Gemini API evaluation error: {e}")
            return fallback     # Fallback if API fails


# ---------------------------------------------------------------------------
# SCRAPING LOGIC - pre-filter with keywords before calling Gemini
# ---------------------------------------------------------------------------


# Postings must clear this weaker keyword bar before burning a Gemini API call.
# Lower than MIN_MATCH_SCORE on purpose — this is just a cheap pre-filter, not
# the actual accept/reject decision.
GEMINI_PREFILTER_SCORE = 15


# ---------------------------------------------------------------------------
# SCRAPING LOGIC - Scoring
# ---------------------------------------------------------------------------


def score_job(title, description=""):
    """Return a job's total score, or ``None`` when it is not eligible."""
    evaluation = evaluate_job(title, description)
    return None if evaluation is None else evaluation["match_score"]


def matches_filters(title, description=""):
    """Maintain the filtering interface while enforcing the score threshold."""
    score = score_job(title, description)
    return score is not None and score >= MIN_MATCH_SCORE


def posting_key(posting):
    """Return an ID that remains unique across companies and ATS platforms."""
    return f"{posting['source']}:{posting['company']}:{posting['job_id']}"
 
 
def load_existing_postings():
    """Load previously seen postings using a company-and-source-aware key."""
    if not DATA_FILE.exists():
        return set()
    with open(DATA_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {
            f"{row.get('source', '')}:{row.get('company', '')}:{row['job_id']}"
            for row in reader
        }


def ensure_posting_schema():
    """Add newly introduced CSV columns without discarding posting history."""
    if not DATA_FILE.exists():
        return

    with open(DATA_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == POSTING_FIELDS:
            return
        rows = list(reader)

    with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=POSTING_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in POSTING_FIELDS})
 
 
def append_postings(postings):
    """Append new postings to the CSV log, creating it with headers if needed."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    ensure_posting_schema()
    file_exists = DATA_FILE.exists()
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=POSTING_FIELDS, extrasaction="ignore")        ## added "extrasaction="ignore""
        if not file_exists:
            writer.writeheader()
        for p in postings:
            writer.writerow(p)
 
 
# ---------------------------------------------------------------------------
# NOTIFICATIONS
# ---------------------------------------------------------------------------
 
 
def send_slack_notification(new_postings):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return
    lines = [f"*{p['company']}* — {p['title']} ({p['location']})\n{p['url']}" for p in new_postings]
    text = f":briefcase: *{len(new_postings)} new posting(s) found:*\n\n" + "\n\n".join(lines)
    try:
        requests.post(webhook_url, json={"text": text}, timeout=10)
    except requests.RequestException as e:
        print(f"  [!] Slack notification failed: {e}")
 
 
def send_email_notification(new_postings):
    smtp_gmail_user = os.environ.get("SMTP_GMAIL_USER")
    smtp_gmail_pass = os.environ.get("SMTP_GMAIL_PASS")
    notify_to = os.environ.get("NOTIFY_GMAIL_EMAIL")
    
    if not (smtp_gmail_user and smtp_gmail_pass and notify_to):
        print("  [!] Missing email credentials in environment variables. Skipping email.")
        return

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    run_time = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")

    if new_postings:
        lines = [f"{p['company']} — {p['title']} ({p['location']})\n{p['url']}" for p in new_postings]
        body = (
            f"Run completed: {run_time}\n\n"
            f"{len(new_postings)} new posting(s) found:\n\n" + "\n\n".join(lines)
        )
        subject = f"[Job Scraper] {len(new_postings)} new posting(s) — {run_time}"
    else:
        body = f"Run completed: {run_time}\n\nNo new postings found this run."
        subject = f"[Job Scraper] No new postings — {run_time}"
 
    msg = MIMEText(body)
    # msg["Subject"] = f"[Job Scraper] {len(new_postings)} new posting(s)"
    msg["Subject"] = subject
    msg["From"] = smtp_gmail_user
    msg["To"] = notify_to
 
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_gmail_user, smtp_gmail_pass)
            server.sendmail(smtp_gmail_user, [notify_to], msg.as_string())
        print("  [+] Email notification sent successfully!")
    except Exception as e:
        print(f"  [!] Email notification failed: {e}")
 
def sync_to_google_sheets(new_postings):
    """Append new postings to a Google Sheet, mirroring the CSV log."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")

    if not (creds_json and sheet_id):
        print("  [!] Missing Google Sheets credentials or sheet ID. Skipping sync.")
        return

    try:
        
        import json as jsonlib
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = jsonlib.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sheet_id).sheet1

        existing_header = sheet.row_values(1)
        if not existing_header:
            sheet.append_row(POSTING_FIELDS)
        elif existing_header != POSTING_FIELDS:
            # Existing data keeps its original column positions because new
            # score fields are appended after date_found.
            sheet.update([POSTING_FIELDS], "A1")

        rows = [[p.get(field, "") for field in POSTING_FIELDS] for p in new_postings]
        sheet.append_rows(rows)
        print(f"  [+] Synced {len(rows)} posting(s) to Google Sheets.")
    except Exception as e:
        print(f"  [!] Google Sheets sync failed: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
 
 
def main():
    from datetime import datetime, timezone
 
    print("Fetching postings...")
    all_jobs = []
# replaced with so below
 
#    for company in GREENHOUSE_COMPANIES:
#        print(f" - Greenhouse: {company}")
#        all_jobs.extend(fetch_greenhouse_jobs(company))
#    for company in LEVER_COMPANIES:
#        print(f" - Lever: {company}")
#        all_jobs.extend(fetch_lever_jobs(company))
 
    for company in COMPANIES:
        
        if not company["enabled"]:
            continue
        
        print(f"Searching {company['name']}...")
 
        if company["platform"] == "greenhouse":
            all_jobs.extend(fetch_greenhouse_jobs(company))
 
        elif company["platform"] == "lever":
            all_jobs.extend(fetch_lever_jobs(company))

        elif company["platform"] == "workday":
            all_jobs.extend(fetch_workday_jobs(company))

        else:
            print(f"  [!] Unsupported platform '{company['platform']}' for {company['name']}, skipping.")

    existing_ids = load_existing_postings()
    new_postings = []
    for job in all_jobs:
        # Step 1: Fast rule check to filter out non-intern/entry roles
        if not is_role_candidate(job["title"]):
            continue

        # Step 2: Skip postings we've already logged
        if posting_key(job) in existing_ids:
            continue

        # Step 3: Cheap keyword pre-filter — only spend a Gemini call on
        # postings that already show some relevance signal
        keyword_evaluation = evaluate_job(job["title"], job.get("description", ""))
        if keyword_evaluation is None or keyword_evaluation["match_score"] < GEMINI_PREFILTER_SCORE:
            continue

        # Step 4: Send the pre-filtered candidate to Gemini (or fall back to
        # the keyword result if no API key / Gemini fails)
        evaluation = evaluate_job_with_gemini(
            job["title"], job.get("description", ""), keyword_evaluation=keyword_evaluation
        )

        if evaluation is not None and evaluation["match_score"] >= MIN_MATCH_SCORE:
            job.update(evaluation)
            new_postings.append(job)

    print(f"Found {len(new_postings)} new posting(s) matching evaluation filters.")

    # new_postings = [j for j in matched if posting_key(j) not in existing_ids]
 
    if not new_postings:
        print("No new postings since last run.")
 
        # test email template
        # print("Sending test email to verify credentials...")
        # test_posting = [{
            # "company": "Test Company",
            # "title": "Software Engineer Intern (TEST)",
            # "location": "Remote",
            # "url": "https://example.com"
        # }]
        # send_email_notification(test_posting)
        
        send_email_notification([])
        return
 
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for p in new_postings:
        p["date_found"] = today
 
    append_postings(new_postings)
    print(f"Logged {len(new_postings)} new posting(s) to {DATA_FILE}")
 
    send_slack_notification(new_postings)
    sync_to_google_sheets(new_postings)
    send_email_notification(new_postings)
 
 
if __name__ == "__main__":
    main()
