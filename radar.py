#!/usr/bin/env python3
"""Job radar: checks target companies' public job feeds and pings new matching roles.

No AI involved, so it is fast and free. Runs on GitHub Actions every 30 minutes.
State is kept in feeds.json (where each company's jobs live), seen.json (jobs
already sent) and matches.csv (log of every match).
"""
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANIES_FILE = os.path.join(HERE, "companies.txt")
FEEDS_FILE = os.path.join(HERE, "feeds.json")
SEEN_FILE = os.path.join(HERE, "seen.json")
MATCHES_FILE = os.path.join(HERE, "matches.csv")
FUNDED_URL = "https://theberlinlife.com/funded-startups-hiring-in-germany/"

REDISCOVER_DAYS = 7      # retry companies with no feed found once a week
FUNDED_REFRESH_HOURS = 24
TIMEOUT = 15
UA = "Mozilla/5.0 (job-radar; personal use)"

# ---------------------------------------------------------------- filters
# A role matches if its TITLE hits an include pattern, hits no exclude
# pattern, and its LOCATION looks like Berlin / Germany / remote-Europe.
INCLUDE = [
    r"\bcrm\b", r"lifecycle", r"retention", r"\bgrowth\b", r"customer engagement",
    r"email marketing", r"customer marketing", r"marketing automation", r"martech",
    r"marketing op", r"revenue op", r"\brevops\b", r"\bgtm\b", r"go[- ]to[- ]market",
    r"product manager", r"product owner", r"product op", r"associate product",
    r"\bapm\b", r"implementation", r"onboarding", r"solutions? (consultant|manager|specialist)",
    r"founder'?s? associate", r"business operations", r"\bbizops\b",
    r"strategy (&|and) operations", r"chief of staff", r"\bbraze\b", r"loyalty",
]
EXCLUDE = [
    r"\bhead of\b", r"\bdirector\b", r"\bvp\b", r"vice president", r"\bprincipal\b",
    r"\bstaff\b", r"\bengineer", r"developer", r"data scientist", r"werkstudent",
    r"working student", r"\bstudent\b", r"\bsales\b", r"account executive",
    r"paid (social|search|media)", r"\bsea\b", r"\bseo\b", r"performance market",
    r"recruit", r"\bhr\b", r"talent",
]
# Internships only count for product roles.
INTERN = r"\bintern(ship)?\b|\bpraktikum\b|\bpraktikant"

LOC_OK = r"berlin|germany|deutschland"
REMOTE_OK = r"remote|anywhere|hybrid"
REMOTE_REGION = r"europe|\beu\b|emea|germany|dach|cet"
REMOTE_BAD = r"\b(us|usa|united states|canada|india|apac|latam|uk only)\b"


def title_matches(title):
    t = title.lower()
    if not any(re.search(p, t) for p in INCLUDE):
        return False
    if any(re.search(p, t) for p in EXCLUDE):
        return False
    if re.search(INTERN, t) and "product" not in t:
        return False
    return True


def location_matches(loc):
    l = (loc or "").lower()
    if re.search(LOC_OK, l):
        return True
    if re.search(REMOTE_OK, l):
        if re.search(REMOTE_BAD, l):
            return False
        return bool(re.search(REMOTE_REGION, l)) or l.strip() in ("remote", "fully remote")
    return False


# ---------------------------------------------------------------- http
def get(url, accept_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None
    except Exception:
        return None
    if not accept_json:
        return body
    try:
        return json.loads(body)
    except ValueError:
        return None


def norm(s):
    s = s.lower().replace("ö", "o").replace("ü", "u").replace("ä", "a").replace("€", "eur")
    return re.sub(r"[^a-z0-9]", "", s)


# ---------------------------------------------------------------- feeds
# Each fetcher returns (jobs, board_name_or_None) or None if the board
# does not exist. A job is a dict: id, title, location, url, posted.

def gh_fetch(slug):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not data or "jobs" not in data:
        return None
    meta = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}") or {}
    jobs = []
    for j in data["jobs"]:
        locs = [(j.get("location") or {}).get("name", "")]
        locs += [o.get("name", "") for o in j.get("offices", []) if isinstance(o, dict)]
        jobs.append({"id": str(j["id"]), "title": j.get("title", ""),
                     "location": " / ".join(x for x in locs if x),
                     "url": j.get("absolute_url", ""),
                     "posted": j.get("first_published") or j.get("updated_at", "")})
    return jobs, meta.get("name")


def lever_fetch(slug, eu=False):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    data = get(f"https://{host}/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return None
    jobs = []
    for j in data:
        cat = j.get("categories") or {}
        locs = [cat.get("location", "")] + list(cat.get("allLocations") or [])
        if j.get("workplaceType") == "remote":
            locs.append("Remote")
        ts = j.get("createdAt")
        posted = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else ""
        jobs.append({"id": j["id"], "title": j.get("text", ""),
                     "location": " / ".join(dict.fromkeys(x for x in locs if x)),
                     "url": j.get("hostedUrl", ""), "posted": posted})
    return jobs, None


def ashby_fetch(slug):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        locs = [j.get("location", "")]
        locs += [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        jobs.append({"id": j.get("id", j.get("jobUrl", "")), "title": j.get("title", ""),
                     "location": " / ".join(x for x in locs if x),
                     "url": j.get("jobUrl", ""), "posted": j.get("publishedAt", "")})
    return jobs, None


def personio_fetch(slug):
    for tld in ("de", "com"):
        body = get(f"https://{slug}.jobs.personio.{tld}/xml", accept_json=False)
        if not body or "<workzag-jobs" not in body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        jobs = []
        for p in root.findall("position"):
            pid = p.findtext("id", "")
            offices = [p.findtext("office", "")]
            offices += [o.text or "" for o in p.findall("additionalOffices/office")]
            jobs.append({"id": pid, "title": p.findtext("name", ""),
                         "location": " / ".join(x for x in offices if x),
                         "url": f"https://{slug}.jobs.personio.{tld}/job/{pid}",
                         "posted": p.findtext("createdAt", "")})
        return jobs, None
    return None


def smartrecruiters_fetch(slug):
    jobs, offset, name = [], 0, None
    while True:
        data = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        if not data or "content" not in data:
            return None if offset == 0 else (jobs, name)
        for j in data["content"]:
            loc = j.get("location") or {}
            parts = [loc.get("city", ""), loc.get("country", "")]
            if loc.get("remote"):
                parts.append("Remote")
            name = name or (j.get("company") or {}).get("name")
            jobs.append({"id": j["id"], "title": j.get("name", ""),
                         "location": ", ".join(x for x in parts if x),
                         "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                         "posted": j.get("releasedDate", "")})
        offset += 100
        if offset >= data.get("totalFound", 0) or offset > 2000:
            return jobs, name


def workable_fetch(slug):
    data = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        parts = [j.get("city", ""), j.get("country", "")]
        if j.get("telecommuting"):
            parts.append("Remote")
        jobs.append({"id": j.get("shortcode", j.get("url", "")), "title": j.get("title", ""),
                     "location": ", ".join(x for x in parts if x),
                     "url": j.get("url") or j.get("shortlink", ""),
                     "posted": j.get("published_on", "")})
    return jobs, data.get("name")


def recruitee_fetch(slug):
    data = get(f"https://{slug}.recruitee.com/api/offers/")
    if not data or "offers" not in data:
        return None
    jobs, name = [], None
    for j in data["offers"]:
        name = name or j.get("company_name")
        parts = [j.get("city", ""), j.get("country", "")]
        if j.get("remote"):
            parts.append("Remote")
        jobs.append({"id": str(j["id"]), "title": j.get("title", ""),
                     "location": ", ".join(x for x in parts if x),
                     "url": j.get("careers_url", ""), "posted": j.get("published_at", "")})
    return jobs, name


FETCHERS = {
    "greenhouse": gh_fetch,
    "lever": lever_fetch,
    "lever_eu": lambda s: lever_fetch(s, eu=True),
    "ashby": ashby_fetch,
    "personio": personio_fetch,
    "smartrecruiters": smartrecruiters_fetch,
    "workable": workable_fetch,
    "recruitee": recruitee_fetch,
}
# Boards that report the company's name, so a guessed slug can be checked.
NAMED = {"greenhouse", "smartrecruiters", "workable", "recruitee"}


# ---------------------------------------------------------------- discovery
def slug_candidates(name):
    base = name.lower().replace("&", "and").replace("€", "eur")
    base = re.sub(r"\b(gmbh|ag|se|group|international|the)\b", " ", base)
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    c = ["".join(words), "-".join(words), words[0]]
    out = []
    for s in c:
        if s and s not in out:
            out.append(s)
    return out


SUFFIXES = r"\b(gmbh|ag|se|inc|ltd|llc|bv|sa|sas|group|holding|technologies|labs|bank|co|kg|ug)\b"


def name_ok(company, board_name):
    """Board name must be the company name, give or take a legal suffix.
    Long names may also be a prefix (e.g. 'Trade Republic Bank')."""
    if not board_name:
        return False
    a = norm(re.sub(SUFFIXES, " ", company.lower()))
    b = norm(re.sub(SUFFIXES, " ", board_name.lower()))
    if not a or not b:
        return False
    return a == b or (len(a) >= 8 and b.startswith(a))


def discover(company):
    """Try every board type with a few slug guesses. Short or generic names
    are only accepted from boards that confirm the company name."""
    cands = slug_candidates(company)
    distinctive = len(norm(company)) >= 6
    for slug in cands:
        for ats, fn in FETCHERS.items():
            if ats == "lever_eu" and not distinctive:
                continue
            res = fn(slug)
            if not res:
                continue
            jobs, board_name = res
            if ats in NAMED and board_name:
                if name_ok(company, board_name):
                    return {"ats": ats, "slug": slug}
                continue
            if distinctive and slug == cands[0] and jobs:
                return {"ats": ats, "slug": slug}
    return None


# ---------------------------------------------------------------- companies
def read_companies():
    out = []
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, pin = [x.strip() for x in line.split("|", 1)]
                ats, slug = [x.strip() for x in pin.split(":", 1)]
                out.append((name, {"ats": ats, "slug": slug}))
            else:
                out.append((line, None))
    return out


def funded_berlin_startups():
    body = get(FUNDED_URL, accept_json=False)
    if not body:
        return []
    names = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if len(cells) < 3 or cells[0].lower() in ("company", "startup", "name"):
            continue
        if any("berlin" in c.lower() for c in cells[1:]):
            names.append(cells[0])
    return list(dict.fromkeys(n for n in names if 1 < len(n) < 60))


# ---------------------------------------------------------------- state
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)


def now():
    return datetime.now(timezone.utc)


def older_than(iso, days=0, hours=0):
    if not iso:
        return True
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return True
    return (now() - t).total_seconds() > days * 86400 + hours * 3600


# ---------------------------------------------------------------- notify
def notify(text):
    sent = False
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=TIMEOUT)
            sent = True
        except Exception as e:
            print("telegram failed:", e, file=sys.stderr)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                                     headers={"Title": "New job matches", "Tags": "briefcase"})
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT)
            sent = True
        except Exception as e:
            print("ntfy failed:", e, file=sys.stderr)
    if not sent:
        print("--- notification (no channel configured) ---\n" + text)


def fmt(job):
    return f"{job['company']} — {job['title']}\n{job['location']}\n{job['url']}"


def send_batches(header, jobs, per_msg=8):
    for i in range(0, len(jobs), per_msg):
        chunk = jobs[i:i + per_msg]
        head = header if i == 0 else f"{header} (cont.)"
        notify(head + "\n\n" + "\n\n".join(fmt(j) for j in chunk))
        time.sleep(1)


# ---------------------------------------------------------------- main
def main():
    feeds = load(FEEDS_FILE, {})
    seen = load(SEEN_FILE, {})
    first_run = not seen

    companies = read_companies()
    names = {n for n, _ in companies}

    # Recently funded Berlin startups, refreshed daily.
    meta = feeds.setdefault("_meta", {})
    if older_than(meta.get("funded_checked"), hours=FUNDED_REFRESH_HOURS):
        funded = funded_berlin_startups()
        if funded:
            meta["funded"] = funded
        meta["funded_checked"] = now().isoformat()
    for n in meta.get("funded", []):
        if n not in names:
            companies.append((n, None))
            names.add(n)

    # Work out where each company's jobs live.
    def resolve(item):
        name, pin = item
        if pin:
            return name, {**pin, "pinned": True, "checked": now().isoformat()}
        known = feeds.get(name)
        if known and known.get("ats"):
            return name, known
        if known and not older_than(known.get("checked"), days=REDISCOVER_DAYS):
            return name, known
        found = discover(name) or {}
        return name, {**found, "checked": now().isoformat()}

    with ThreadPoolExecutor(max_workers=16) as ex:
        for name, info in ex.map(resolve, companies):
            feeds[name] = info

    # Fetch all jobs.
    active = [(n, feeds[n]) for n, _ in companies if feeds.get(n, {}).get("ats")]

    def pull(item):
        name, info = item
        res = FETCHERS[info["ats"]](info["slug"])
        return name, (res[0] if res else None)

    matches, broken = [], []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for name, jobs in ex.map(pull, active):
            if jobs is None:
                broken.append(name)
                continue
            for j in jobs:
                if title_matches(j["title"]) and location_matches(j["location"]):
                    key = f"{feeds[name]['ats']}:{feeds[name]['slug']}:{j['id']}"
                    matches.append({**j, "company": name, "key": key})

    # A pinned feed that stopped working gets rediscovered next run.
    for name in broken:
        if not feeds[name].get("pinned"):
            feeds[name] = {"checked": "1970-01-01T00:00:00+00:00"}

    new = [m for m in matches if m["key"] not in seen]
    stamp = now().isoformat()
    for m in new:
        seen[m["key"]] = stamp

    if new:
        exists = os.path.exists(MATCHES_FILE)
        with open(MATCHES_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["found_utc", "company", "title", "location", "url", "posted"])
            for m in new:
                w.writerow([stamp, m["company"], m["title"], m["location"], m["url"], m["posted"]])

    if first_run:
        send_batches(f"Job radar is live. {len(new)} matching roles are open right now:", new)
    elif new:
        send_batches(f"{len(new)} new job match{'es' if len(new) > 1 else ''}:", new)

    save(FEEDS_FILE, feeds)
    save(SEEN_FILE, seen)

    covered = sum(1 for n, _ in companies if feeds.get(n, {}).get("ats"))
    missing = sorted(n for n, _ in companies if not feeds.get(n, {}).get("ats"))
    with open(os.path.join(HERE, "coverage.md"), "w", encoding="utf-8") as f:
        f.write(f"# Coverage\n\nUpdated {stamp}\n\n{covered} of {len(companies)} companies have a job feed "
                f"the radar checks every run.\n\n## No public feed found ({len(missing)})\n\n"
                "These need a pinned feed in companies.txt or are covered by Claude's daily check.\n\n")
        f.writelines(f"- {n}\n" for n in missing)
    print(f"companies={len(companies)} covered={covered} matches={len(matches)} new={len(new)}")


if __name__ == "__main__":
    main()
