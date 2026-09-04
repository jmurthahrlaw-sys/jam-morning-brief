# JAM Morning Brief

A personal daily news system inspired by the Rally News / Rundown workflow:

**collect → deduplicate → editorial AI → HTML email → optional audio briefing**

The project is designed around the editorial profile in `editorial_profile.txt` and gives extra weight to Minnesota news, U.S. Supreme Court activity, federal courts relevant to Minnesota and California, Minnesota law, California law/employment law, and entertainment/culture.

## What is already built in this starter package

- Hourly RSS/Google News collection
- Personalized Lexology daily-newsfeed ingestion (URL stored privately as a GitHub Secret)
- Optional ELINfonet Daily Employment Law Update ingestion directly from email via IMAP
- 10-day rolling story store in `data/news.json`
- Exact and near-duplicate headline filtering
- OpenRouter-powered daily editorial selection and synthesis
- Legal-specific fields for court, case, holding/development, and practical effect
- Polished HTML + plain-text email output
- Brevo transactional email delivery
- Optional 8–12 minute audio briefing using OpenAI text-to-speech
- GitHub Actions workflows for hourly collection and daily delivery

## Important security rule

This repository is public. **Never paste API keys, email passwords, or your personalized Lexology feed URL into a file.** Put them in **GitHub → Settings → Secrets and variables → Actions → Repository secrets**.

## Files

- `editorial_profile.txt` — your editorial rules
- `sources.json` — public feeds/searches
- `scraper.py` — news collector
- `dedupe.py` — duplicate filtering
- `daily_digest.py` — AI selection, synthesis, HTML formatting
- `audio_brief.py` — conversational script + optional MP3
- `send_email.py` — Brevo delivery
- `.github/workflows/scrape-news.yml` — hourly collector
- `.github/workflows/daily-brief.yml` — morning email/audio job

## Setup — do these in order

### Step 1 — Put the starter files in the GitHub repository

Upload the contents of this package to the root of your repository. Keep the `.github/workflows/` folder structure exactly as supplied.

### Step 2 — Add the OpenRouter key

GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**

Create:

`OPENROUTER_API_KEY`

Value: your OpenRouter API key.

Optionally under the **Variables** tab create:

`OPENROUTER_MODEL`

Suggested initial value:

`openai/gpt-4.1-mini`

If the variable is omitted, the Python code uses that model as its default.

### Step 3 — Add Lexology privately

Do **not** put the personalized Lexology URL in `sources.json` because this is a public repository.

Create repository secret:

`LEXOLOGY_NEWSFEED_URL`

Paste your full daily Lexology URL as the value. The script automatically replaces the `d=` date parameter with the current date while preserving your feed identifier.

### Step 4 — Optional: connect the ELINfonet daily email

The collector can pull the Employment Law Information Network email directly from an IMAP inbox.

For Gmail, create these repository secrets:

- `IMAP_HOST` = `imap.gmail.com`
- `IMAP_USER` = the email address receiving ELINfonet
- `IMAP_APP_PASSWORD` = a Google App Password, **not your normal Google password**

The sender is already set to `update@elinfonet.com` in the workflow.

If your ELINfonet email goes to a non-Gmail mailbox, use that provider's IMAP host/app-password method instead.

You may skip this step at first. Everything else will still run.

### Step 5 — Set up Brevo for email

Create a Brevo account and verify a sender email/domain. Then add these GitHub repository secrets:

- `BREVO_API_KEY`
- `BRIEF_TO_EMAIL` — the address where you want the morning brief
- `BRIEF_FROM_EMAIL` — the sender address you verified in Brevo

Optional GitHub repository variable:

- `BRIEF_TO_NAME`

The code sends the generated HTML directly through Brevo's transactional email API.

### Step 6 — Optional: turn on the audio briefing

Add repository secret:

`OPENAI_API_KEY`

The workflow uses `gpt-4o-mini-tts` by default and creates an MP3. If the audio file is small enough, it is attached to the Brevo email. If this secret is omitted, the email still works; only the audio step is skipped.

### Step 7 — Test manually before waiting for the schedule

Go to the repository's **Actions** tab.

1. Open **Collect news**.
2. Click **Run workflow**.
3. Wait for a green checkmark.
4. Confirm that `data/news.json` now contains stories.
5. Open **Send daily morning brief**.
6. Click **Run workflow**.
7. Confirm the email arrives.

## Daily timing

The starter workflow runs the morning job at `12:05 UTC`, which is approximately 7:05 AM Central during daylight-saving time and 6:05 AM during standard time.

After the system is working, adjust the schedule to your preferred delivery time and, if desired, add daylight-saving-safe scheduling.

## How the legal sources fit together

The system intentionally uses more than one type of source:

1. **Lexology** for a broad daily legal-news scan.
2. **ELINfonet** for employment-law alerts and practitioner material.
3. **Targeted news searches** for U.S. Supreme Court, District of Minnesota/Eighth Circuit, California federal courts/Ninth Circuit, Minnesota appellate law, and California appellate/employment law.
4. The editorial profile tells the AI to prefer primary court/agency material when available and not to overstate routine rulings.

A later version can add direct court-specific collectors for official Supreme Court, Minnesota appellate, and California published-opinion pages after we test the first working edition.

## Refining the briefing

The most important file over time is `editorial_profile.txt`.

When the briefing includes something you do not care about, add a rule explaining what to exclude. When it misses a type of story you wanted, add a rule explaining what to prioritize. This is how the briefing becomes increasingly personalized without rewriting the collector.
