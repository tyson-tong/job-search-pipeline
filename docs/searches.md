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
| `max_results`, `max_cost_usd`, `radius_km` | `30`, `0.50`, `25` | Usually set once under `[defaults]` |
| `exclude_title_terms` | `["Internship", "Fresh Grad", "Graduate"]` | Jobs whose title contains any of these words are stored as `filtered` and never reach the Inbox. Whole words, any capitalisation and punctuation ("Graduate" excludes "Graduate Analyst", not "Postgraduate"). Usually set under `[defaults]`; imports apply it to new jobs, and `jobflow update-searches` to jobs already in the Inbox |
| `include_title_terms` | `["Quant Analyst", "Quantitative Analyst"]` | Overrides `exclude_title_terms`: a title containing any of these is kept even if it also contains an excluded term, so with `"Analyst"` excluded, "Quant Analyst – Equities" stays while "Credit Analyst" is filtered. Matched the same way (whole words, any capitalisation). Has no effect on titles that match no excluded term |

## Countries and cities

Cities must come from this list (any capitalisation); a misspelt or unlisted city is rejected rather than searched. Use `[]` to search the whole country.

| Code | Country | Cities | Sites |
|---|---|---|---|
| `AU` | Australia | Sydney, Melbourne, Brisbane, Perth, Adelaide, Canberra | all three |
| `NZ` | New Zealand | Auckland, Christchurch, Wellington | all three |
| `SG` | Singapore | Singapore | all three |
| `HK` | Hong Kong | Hong Kong | all three |
| `GB` (or `UK`) | United Kingdom | London, Manchester, Birmingham, Edinburgh, Glasgow, Leeds, Bristol | LinkedIn, Indeed |
| `US` | United States | New York, San Francisco, Los Angeles, Chicago, Boston, Seattle, Houston | LinkedIn, Indeed |

Seek has no UK or US site, so it skips those countries. The lists are `CITIES` in `src/jobflow/ingest/options.py`; to add a city or country, edit it there.

## Salary in different currencies

For different salary cutoffs in AUD, SGD, and HKD, make one `[[search]]` entry per country and put `salary_min`, `salary_max`, and `salary_period` inside that country’s entry. The amounts are in the posting’s local currency and are not converted. (The tracker's own minimum-salary filter is different: it converts each job's salary to US dollars.) Leave salary fields out to disable salary filtering for that country. Separate searches may increase LinkedIn scraper runs because LinkedIn runs once per search.

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

## Which site applies which filter

The three sites don't support the same filters, so each is translated to what the site can do:

| | LinkedIn | Indeed | Seek |
|---|---|---|---|
| Terms, country, city, date posted | on site | on site | on site |
| Salary | after import | after import | after import |
| Job type, work arrangement | after import | after import | on site |
| Seek classifications | no | no | on site |
| Excluded title terms | after import | after import | after import |

"On site" filters are applied by the job site, so you don't pay for excluded jobs. "After import" filters are applied when results reach your database. They only exclude a job whose salary, job type, or arrangement is listed and doesn't match, or whose title contains an excluded term; the job is stored with status `filtered` and the reason, and appears in the tracker's Filtered tab. LinkedIn has moved to AI search, which dropped its salary, job-type, and workplace filters, and the Indeed actor doesn't offer them. LinkedIn results rarely list a salary and never say whether a job is remote, so in practice those filters seldom remove LinkedIn jobs. Salary bounds are applied after import for all three sites. Missing salary data is kept; Seek is deliberately scraped without salary bounds so it cannot hide salaryless postings upstream. This can increase the number of Seek results and Apify usage.

## Checking your searches

Check the result in the tracker: open `jobflow web` and the **Searches** tab. It validates every value (listing the allowed ones if something is wrong), shows each Apify run with the exact input each actor receives, and lists all allowed values; click **Reload** after editing. Nothing runs from that page. All option values are constants in `src/jobflow/ingest/options.py`.

## Cost

LinkedIn makes one run per search, with a result cap per term and location; Indeed and Seek make one run per term and location. The actors charge per result, including jobs you already have, so keep `posted_within_days = 1` for a daily schedule and set `max_results`. Each run's actual cost is recorded in `scraper_runs.cost_usd`.
