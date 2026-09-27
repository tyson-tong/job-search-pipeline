# How jobflow's rules work

The rules behind merging duplicates, flagging similar jobs, scoring, checking tailored CVs and cover letters, and ranking skill gaps. For a diagram of every component and table, open [structure.html](structure.html) in a browser.

## Duplicates

Each scraper's output is first converted to the same fields (`src/jobflow/ingest/sources.py`). A job then counts as already seen if any of these match, in order:
1. Same site and job ID (or, for a posting without an ID, the same URL without tracking parameters), checked against every site's copy seen so far, not just the first.
2. Identical company, title, location, and description.
3. Same employer and title, ignoring suffixes like "Pty Ltd", in the same city, first seen in the last 90 days, with descriptions that share at least half their words. This catches the same job on LinkedIn, Indeed, and Seek, and reposts with a new ID. "Same city" only rules out a match when both postings name different places, such as Sydney and Melbourne; a missing or country-only location doesn't. Suburbs count as different places, so a Parramatta and a Sydney listing of one job stay separate unless rule 4 applies.
4. The same title (ignoring case and punctuation), in the same country, first seen in the last 90 days, with an ad at least 95% the same words, whatever the employer's spelling or the city. This catches one ad listed under slightly different employer names or districts. Short ads (under 30 distinct words) never match this way, a different title never does (employers reuse one ad for different roles), and neither does one ad for offices in two countries. A job's country is the one its search run covered, or else the country named in its location; an unknown country doesn't block a match.

The job keeps the first site's details (and the longest description). Each site's copy, with its own ID and link, is kept in the `job_sources` table, so later sightings match by ID and you can open the job on any site it was listed on. Every sighting is also recorded with the scraper run it came from.

## Similar jobs

Merging is kept strict so two different jobs are never combined, which means some copies stay separate: a recruiter re-post with an edited ad, a reworded title, or a suburb instead of the city. As a safety net, the tracker compares each job with every job first seen in the last 180 days in the same country, ignoring employer and city: titles sharing at least 60% of their words and ads sharing at least 40% are listed as similar jobs, and as highly similar when the ads share at least 80%. They are never merged automatically. The same ad in another country (one role advertised for offices in Sydney and Hong Kong, say) is not listed as similar: it is placed next to the job in the list with a blue bar (similar jobs get an orange one), tagged "Same ad in" that country, and listed separately in the job panel.

## Match scores

The LLM lists the job's requirements, marks each required or preferred, and labels it matched, partial, or not evidenced in each CV, with CV quotes. Every quote is checked against the CV file. A "matched" item whose quote isn't found becomes "partial", and a "partial" one becomes "not evidenced". "Not evidenced" means the CV doesn't show it, not that you lack it. The score is calculated, not asked of the LLM: matched counts 1, partial 0.5, not evidenced 0, averaged within required and within preferred, then weighted by `required_weight` and `preferred_weight` in `config/profile.toml`. Use scores for ranking, not as a prediction.

## Tailored CVs

Only the CV's `cv` section goes to the LLM; design, locale, and settings are copied from your file. The draft is then compared with your original CV. These count as errors, and the PDF is not rendered:
- changed contact details
- new or altered employers, positions, dates, or education
- a removed employer or education entry
- numbers that aren't in the original

New capitalised or technical terms, such as a tool name, are warnings. The check catches common problems; it does not replace reading the draft.

With `--format docx` or `latex`, the CV file is laid out in code from the checked YAML (`candidate/documents.py`), never by the LLM, so it says exactly what passed the check. Like RenderCV, these layouts never split an entry across pages.

The length is always measured on RenderCV's PDF, whatever `--format` is, so a `.docx` or `.tex` is fitted to RenderCV's layout and its own page count may differ. If that PDF runs past 1 page, or the `--max-pages N` given to `tailor-cv`, the LLM ranks the bullets from least to most relevant to the job, and the fewest of the least relevant that makes it fit are cut, found by re-rendering with different numbers cut (in a temporary folder) (a binary search, so about 7 renders even for 50 bullets). If it can't fit even with every entry down to one bullet, all those cuts are kept and you get a warning. The LLM only ranks; the cutting is done in code, so nothing is rewritten and every entry keeps at least one bullet. `CV_REVIEW.md` lists what was cut.

Since an entry is never split, one too long for the space left moves whole to the next page. A page other than the last that is at least 30% empty at the bottom because of this gets a warning (for RenderCV's layout); cutting a few of that entry's bullets pulls it up.

## Cover letters and the review

The cover letter may draw only on your CV, the job ad, and the company research. A number found in none of them is an error; a new capitalised or technical term is a warning, as is a `[placeholder]` left in. A letter with errors is not written as `cover-letter.docx`; its text is in `COVER_LETTER_REVIEW.md` to fix. Cover letters are optional (`jobflow cover-letter`). For both CVs and letters, a reviewer prompted as the job's hiring manager critiques the first draft and revises it. The revision is checked the same way and kept only if it has no more errors than the first draft; otherwise the first draft is kept and the review file (`CV_REVIEW.md` or `COVER_LETTER_REVIEW.md`) says so.

## Skill gaps

Each scored job contributes the requirements its best-matching CV didn't fully show. The LLM only groups them into skills (the same skill worded differently) and suggests how to close each gap; it can't change the counts. A skill's rank is the sum over its requirements of 1 for required or 0.5 for preferred, halved when the CV partly shows it. Ids the LLM makes up or repeats are ignored.

## Failures

If one Apify run fails, the error is recorded in the `scraper_runs` table, with the search name and the exact input, and the other runs still go ahead. The GitHub workflow then shows as failed, so you get GitHub's failure email.
