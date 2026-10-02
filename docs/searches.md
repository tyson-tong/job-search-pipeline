# Your searches

`config/searches.toml` says what the daily import looks for. `jobflow init` creates it from the template with one example search, for Sydney; replace it with your own before the daily import runs. After each edit, run `uv run jobflow update-searches` (see the README's [Changing your searches](../README.md#changing-your-searches)).

Settings under `[defaults]` apply to every search unless a search sets its own. Each `[[search]]` has:

| Setting | Example | Notes |
|---|---|---|
| `name` | `"data-analyst"` | Required and unique; used by `--search` and in run history |
| `terms` | `["data analyst", "bi analyst"]` | Each term is searched separately |
| `locations` | `{ AU = ["Sydney"], SG = ["Singapore"], HK = ["Hong Kong"] }` | Country code → cities from the list below; `[]` = whole country |
| `salary_min`, `salary_max`, `salary_period` | `100000`, `160000`, `"annual"` | Local currency; the period is annual, monthly, or hourly. Jobs without a listed salary are kept |
| `job_types` | `["full-time", "contract"]` | full-time, part-time, contract, casual |
| `work_arrangements` | `["hybrid", "remote"]` | on-site, hybrid, remote |
| `seek_classifications` | `["information-communication-technology"]` | Seek only; the tracker's Searches tab lists them all. Earlier files may call it `classifications`, which still works |
| `sites` | `["seek", "linkedin"]` | Which job sites to search; see below |
| `posted_within_days` | `1` | 1 (24 hours), 7 (1 week), 14 (2 weeks), or 30 (1 month); see below |
| `max_results`, `max_cost_usd`, `radius_km` | `30`, `0.50`, `25` | Usually set once under `[defaults]`. Left out: 50 results, no cost cap (`update-searches` and the Searches tab warn about that), and no radius. Seek returns at most 550 results per run, whatever `max_results` says |
| `exclude_title_terms` | `["Internship", "Fresh Grad", "Graduate"]` | Jobs whose title contains any of these words are stored as `filtered` and never reach the Inbox. Whole words, any capitalisation and punctuation except `+` and `#` ("Graduate" excludes "Graduate Analyst", not "Postgraduate"; "C++" excludes "C++ Developer", not "Series C"). Usually set under `[defaults]`; imports apply it to new jobs, and `jobflow update-searches` to jobs already in the Inbox |
| `include_title_terms` | `["Quant Analyst", "Quantitative Analyst"]` | Overrides `exclude_title_terms`: a title containing any of these is kept even if it also contains an excluded term, so with `"Analyst"` excluded, "Quant Analyst – Equities" stays while "Credit Analyst" is filtered. Matched the same way (whole words, any capitalisation). Has no effect on titles that match no excluded term. `update-searches` and the Searches tab warn about an excluded term that contains a kept one (it could never apply) and about terms listed twice |
| `exclude_companies` | `["Acme", "Randstad"]` | Jobs whose employer's name contains any of these words are stored as `filtered`. Matched like `exclude_title_terms` (whole words, any capitalisation and punctuation), so "Acme" excludes "ACME Pty Ltd" but not "Acmera"; a job with no employer listed is kept. `include_title_terms` don't override it. Usually set under `[defaults]`; imports apply it to new jobs, and `jobflow update-searches` to jobs already in the Inbox |

## Countries and cities

Cities must come from this list (any capitalisation); a misspelt or unlisted city is rejected rather than searched. Use `[]` to search the whole country.

| Code | Country | Cities | Sites |
|---|---|---|---|
| `AU` | Australia | Sydney, Melbourne, Brisbane, Perth, Adelaide, Canberra | all three |
| `NZ` | New Zealand | Auckland, Christchurch, Wellington | all three |
| `SG` | Singapore | Singapore | all three |
| `HK` | Hong Kong | Hong Kong | all three |
| `GB` (or `UK`) | United Kingdom | London, Manchester, Birmingham, Edinburgh, Glasgow, Leeds, Bristol | LinkedIn, Indeed |
| `US` | United States | New York, San Francisco, Los Angeles, Chicago, Boston, Seattle, Houston, San Jose, Dallas, Atlanta, Washington D.C., Minneapolis, Philadelphia, Charlotte, Denver, Miami, San Antonio, Omaha | LinkedIn, Indeed |

Seek has no UK or US site, so it skips those countries. The lists are `CITIES` in `src/jobflow/ingest/options.py`; to add a city or country, edit it there.

## Salary in different currencies

For different salary cutoffs in AUD, SGD, and HKD, make one `[[search]]` entry per country and put `salary_min`, `salary_max`, and `salary_period` inside that country’s entry. The amounts are in the posting’s local currency and are not converted. A posting's salary with no period stated is read by its size: under 300 is hourly pay; then, in Hong Kong and Singapore, under 1,000 is daily and under 200,000 (Hong Kong) or 30,000 (Singapore) monthly; elsewhere, under 3,000 is daily and under 20,000 monthly; anything larger is annual. A range written "$60 - $80k" is 60,000 to 80,000. (The tracker's own minimum-salary filter is different: it converts each job's salary to US dollars.) Leave salary fields out to disable salary filtering for that country. Separate searches may increase LinkedIn scraper runs because LinkedIn runs once per search.

## Job sites

Use any combination of `linkedin`, `indeed`, and `seek`. Set `sites` under `[defaults]` for all searches, and override it in a search that needs different sites. If `sites` is left out everywhere, all three are used. For example, to use only Seek and Indeed, except LinkedIn too for one search:

```toml
[defaults]
sites = ["seek", "indeed"]

[[search]]
name = "data-analyst"
sites = ["seek", "indeed", "linkedin"]
...
```

A site you don't list is never called, so it costs nothing. You don't need an account per site; all three scrapers run on your one Apify account. To try a single site once without editing the file, use `uv run jobflow ingest --site seek`.

## Posted-date window

Use `1` for the daily schedule; a longer window suits a first run. Not every site offers every window:

| | LinkedIn | Indeed | Seek |
|---|---|---|---|
| 24 hours, 1 week | ✓ | ✓ | ✓ |
| 2 weeks | uses 1 month | ✓ | ✓ |
| 1 month | ✓ | uses any time | ✓ |

A site without your window searches the next wider one, so no jobs are missed, and jobs posted before your window are marked `filtered` after import. The tracker's Searches tab notes when this happens. You still pay Apify for those extra results.

For a one-off manual import, `jobflow ingest --posted-within-days 14` overrides the window for every search in that run only. Use `1`, `7`, `14`, or `30`; this does not change the searches file or the copy published to GitHub Actions.

## Which site applies which filter

The three sites don't support the same filters, so each is translated to what the site can do:

| | LinkedIn | Indeed | Seek |
|---|---|---|---|
| Terms, country, city, date posted | on site | on site | on site |
| Salary | after import | after import | after import |
| Job type, work arrangement | after import | after import | on site |
| Seek classifications | no | no | on site |
| Excluded title terms, excluded employers | after import | after import | after import |

"On site" filters are applied by the job site, so you don't pay for excluded jobs. "After import" filters are applied when results reach your database. They only exclude a job whose salary, job type, or arrangement is listed and doesn't match, whose title contains an excluded term, or whose employer is excluded; the job is stored with status `filtered` and the reason, and appears in the tracker's Filtered tab. A job found by several searches is judged by the one that found it first, both on import and by `update-searches`; later sightings are duplicates and aren't filtered again. If that search has since been renamed or removed, `update-searches` judges the job by the exclusions under `[defaults]`. A job you move out of the Filtered tab stays out: `update-searches` never filters it again, even if it later returns to the Inbox. The other way round, `update-searches` brings a Filtered job not yet archived back to the Inbox when the search that found it would no longer filter it (it's judged again by every filter above, from the job as first scraped); if that search has been renamed or removed, only a job filtered by an excluded title term or employer can come back, judged by `[defaults]`. This only matters for settings that differ between searches, such as a salary minimum per country. A filtered job more than 2 weeks past its posted date, or the date it was first seen if that's later (or there's no posted date), is archived: kept in the database with status `archived`, and still matched as a duplicate, but not shown in any tab (a similar-job note may still mention one). 90 days after it's archived, its ad and scraped record are cleared to save space; its sites' IDs and fingerprint stay, so a repost is still recognised. LinkedIn has moved to AI search, which dropped its salary, job-type, and workplace filters, and the Indeed actor doesn't offer them. LinkedIn results rarely list a salary and never say whether a job is remote, so in practice those filters seldom remove LinkedIn jobs. Salary bounds are applied after import for all three sites. Missing salary data is kept; Seek is deliberately scraped without salary bounds so it cannot hide salaryless postings upstream. This can increase the number of Seek results and Apify usage.

## Checking your searches

Check the result in the tracker: open `jobflow web` and the **Searches** tab. It validates every value (listing the allowed ones if something is wrong), shows each Apify run with the exact input each actor receives, and lists all allowed values; click **Reload** after editing. Nothing runs from that page. All option values are constants in `src/jobflow/ingest/options.py`, and Seek's classifications are in `seek_classifications.json` beside it.

## Cost

LinkedIn makes one run per search, with a result cap per term and location; Indeed and Seek make one run per term and location. The actors charge per result, including jobs you already have, so keep `posted_within_days = 1` for a daily schedule and set `max_results`. Each run's actual cost is recorded in `scraper_runs.cost_usd`.
