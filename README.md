# Job radar

Checks the job boards of every company in `companies.txt` (plus Berlin startups recently funded, from theberlinlife.com) every 30 minutes, 06:00–22:30 Berlin time. When a new role matches, you get a notification within about 30 minutes of it going live.

No AI and no cost: it reads the public job feeds (Greenhouse, Lever, Ashby, Personio, SmartRecruiters, Workable, Recruitee) directly.

## What counts as a match
- **Title** mentions CRM, lifecycle, retention, growth, customer/email marketing, marketing automation or ops, RevOps/GTM, product manager/owner/ops, implementation, onboarding, solutions, Founder's Associate, BizOps, strategy & ops or chief of staff.
- **Not** Head of / Director / VP / Principal / Staff, engineering, sales, recruiting, paid media/SEO, or working-student roles. Internships only count if they're product internships.
- **Location** is Berlin, Germany, or remote/hybrid within Europe.

You can change the word lists at the top of `radar.py` (`INCLUDE`, `EXCLUDE`).

## Files the radar keeps up to date
- `feeds.json`: where each company's jobs live (found automatically).
- `coverage.md`: companies with **no** public feed. Big companies on their own career sites (Google, Amazon, SAP, Zalando…) land here; Claude's twice-daily check covers those.
- `matches.csv`: every match ever found.
- `seen.json`: jobs already sent, so you never get one twice.

## Setup (one time, ~5 minutes)
1. Create a **private** GitHub repo, e.g. `job-radar`, and add these files.
2. Pick where notifications go and add it under **Settings → Secrets and variables → Actions → New repository secret**:
   - **ntfy (simplest):** install the free ntfy app, subscribe to a long random topic name (e.g. `ilayda-jobs-7f3k92q`), and add the secret `NTFY_TOPIC` with that name.
   - **Telegram:** add `TELEGRAM_BOT_TOKEN` (from BotFather) and `TELEGRAM_CHAT_ID`.
3. Go to **Actions → Job radar → Run workflow** to start the first run. It takes a few minutes because it's finding every company's feed, then sends you everything currently open in one go. After that you only hear about new roles.

## Fixing a company
If `coverage.md` lists a company you know uses one of the supported boards, or the radar picked the wrong board, pin it in `companies.txt`:

```
Trade Republic | greenhouse:traderepublicbank
```
