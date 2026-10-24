# Local QDA Tool — Interview Coding

A 100 % local, single-user qualitative data analysis tool for coding interview transcripts.
It implements the coding workflow of a qualitative content analysis (Mayring, 2014): a
deductive–inductive category system, valence and agency per code assignment, function
interactions, memos, retrieval and analysis views, intra-coder reliability, exports and
readable per-interview reports.

The tool ships with an example category system from technological innovation system (TIS)
research: the TIS functions of Bergek et al. (2008), the knowledge split of Hekkert et al.
(2007), function interactions (Hekkert & Negro, 2009; Suurs & Hekkert, 2009) and an agency
dimension following Huang & Malkin (2026). Only the category names and their structure are
included; definitions, coding rules and anchor examples are empty so that you can fill in your
own (see [Adding your own codebook](#adding-your-own-codebook)).

The repository contains no interview data. Nothing leaves your computer: no cloud, no
telemetry, no external CDN. The app binds to `127.0.0.1:8765` only. `data/`, `backups/` and
`exports/` are created at runtime and excluded from git.

## Quick start

```bash
python -m pip install -r requirements.txt   # once (Python 3.11+)
python run.py                                # starts the app and opens the browser
python run.py demo                           # optional: import the two invented sample transcripts
```

Other commands:

| Command | Effect |
|---|---|
| `python run.py --no-browser` | start without opening a browser |
| `python run.py --port=8766` | start on another port, e.g. a test copy next to a running instance |
| `python run.py backup` | write `backups/project_YYYY-MM-DD_HHMMSS.db` now |
| `python run.py export-all` | write every export to `exports/<timestamp>/` |
| `python run.py restore <file>` | restore a backup (safety copy first) |
| `python run.py demo` | import the two invented sample transcripts in `samples/` |
| `python run.py test` | run the test suite (`pytest`) |

If the browser does not open, go to <http://127.0.0.1:8765>. Runs in current Chrome, Edge,
Firefox and Safari on Windows and macOS.

The tool enforces the valence, agency and quotation-block rules (help panel `?` in the coding
view). The coding rules of your own study belong in your study's coding guide and in the
`Codebook` view: every category has a definition, a coding rule and an anchor example.

## Workflow

1. **Import** (`Import` view): one `.md` file per interview (format below). The preview validates the frontmatter, shows paragraph counts, paragraphs with timestamp, confidential / EN-original / quotation-blocked ranges and optional anonymization hits before anything is written. A backup is created after every import.
2. **Code** (`Coding` view): select respondent text, press a shortcut (`1`–`8` = the eight function codes, `s c b i m r e` = supporting codes, `L` = last-used code, `/` = picker) or click a code. Segments may overlap and carry several codes; each code assignment has a valence (+1 / 0 / −1) and an agency from the value list (`GOV-CENTRAL, GOV-PROV, REGULATOR, GRID, SOE, MARKET, ASSOC, RESEARCH, MULTI`, empty = unset), and the segment has a comment and a quote-candidate flag (refused on quotation-blocked text). Paragraph labels show `R ¶n · hh:mm:ss`. Add function interactions from the segment panel. Every action autosaves; `Ctrl+Z` / `Ctrl+Y` undo and redo everything.
3. **Manage the codebook** (`Codebook` view): edit definitions, coding rules and anchor examples, create inductive codes (definition and coding rule required), rename, re-parent, reorder (drag and drop in the coding view), recolour, retire (never delete; choose whether codings stay or move to the parent), merge, and re-code a parent's segments into sub-codes (split support). Every change asks for a one-line reason and is written to the changelog; the pilot-phase toggle marks changes as `pilot`.
4. **Retrieve and analyse** (`Retrieval`, `Analysis` views): filtered segment lists, full-text search with jump-to-position, code × interview matrix, crosstabs by country/role split by valence, co-occurrence, interactions with a function × function matrix per country and Mermaid export, agency matrix, barriers by co-coded function, mutual learning, cross-case DE | CN.
5. **Check reliability** (`Reliability` view): start a re-coding pass some weeks later (existing codings hidden), finish it, and read percent agreement and Cohen's kappa per code with a side-by-side list of disagreements and resolution notes. The primary pass remains the analytic dataset.
6. **Export** (`Export` view or `python run.py export-all`): analysis tables as Markdown/XLSX/CSV, quote bank, interactions, changelog, reliability report, memos, and a full JSON/CSV dump for archiving. Confidential text is never exported; every export warns if a mapped anonymization term still occurs.
7. **Readable reports** (`Readable reports` view): see the next section.

## Readable reports

The `Readable reports` view creates one easy-to-read HTML page per interview: the full
transcript, the coded passages highlighted in the category colour and, next to each answer, a
box per coded segment (segment ID, categories, valence, agency, interactions). The interview
memo follows the transcript. An overview page lists all interviews with segment counts per
category. Open a report in the browser and use *Print → Save as PDF* for a PDF; *Save selected
as files* writes the pages to `exports/<timestamp>/readable_reports/`. The pages are
self-contained (no internet connection needed). Only the main coding pass is shown; segment
comments, quote-candidate flags and confidential text are never included.

### Labels and memo headings

The reports never show segment comments, but they read three fixed markers, so you can steer a
few labels while coding, without new categories and without any change to the database.

**"single-site" label.** Write `(single-site)` anywhere in a segment's comment, exactly like
that: lower case, with the hyphen and the round brackets. The segment's box then shows a grey
**single-site** label next to its segment ID, and the report's legend explains the label and
counts the segments. It is a label, not a category.

```
own battery runs peak shaving, no pool yet (single-site)
```

**Emergent themes.** On a segment coded *Emergent Themes* (key `EMT`), write
`proposal EMT-<Name>` in the comment, for example:

```
proposal EMT-TEST, short description of the theme
```

The box then shows `Theme: EMT-TEST` directly under the Emergent Themes category, and the
legend lists all themes of the interview with their counts. Rules:

* The name is shown exactly as written after `proposal`: letters, digits and hyphens, starting with `EMT-`. `Proposal` and `proposal` both work.
* Use the same spelling every time. `EMT-TEST` and `EMT-Test` count as two different themes.
* Only the first proposal in a comment counts, and only on segments that carry the EMT category. A proposal on any other segment is ignored.
* A theme is a note on the EMT category, not a new category. Create real sub-categories in the `Codebook` view if a theme should become one.

If your codebook uses a different key than `EMT` for emergent themes, or you want other
markers, adapt `SINGLE_SITE_MARKER`, `_EMT_PROPOSAL` and `_emt_theme` in
`app/services/readable.py`.

**Bold headings in the interview memo.** The interview memo is written with the *Memo* button
at the top of the coding view, next to the interview's status. In the report, each line of the
memo becomes its own paragraph. A line that starts with a heading followed by a colon and a
space shows that heading in bold:

```
Strongest function: Market formation, with clear revenue from balancing markets.
Weakest function: Legitimation. Grid operators still see aggregators as a risk.
Open questions: How stable are the revenues in the long run?
```

The heading must start with a capital letter and may contain letters, spaces, `/` and `-`, up
to about 40 characters. Lines without such a heading appear as normal paragraphs, and empty
lines are skipped. Only the current version of the memo is shown, not earlier saved versions.
Segment and code memos are not part of the report.

## Adding your own codebook

**Categories, definitions, coding rules and anchor examples.** Two ways:

* **In the app (simplest).** Start the tool, open the `Codebook` view and edit each category: name, definition, coding rule, anchor example and colour. *+ New code* adds categories (top level or sub-category); categories you do not need are retired, never deleted. Every change asks for a one-line reason and is logged in the codebook changelog. The dashboard lists categories that still lack a definition or anchor example.
* **In `app/seed.py`, before the first start.** The list `SEED_CODES` holds the starting codebook, one entry per category:
  `(code_key, name, parent_key, type, definition, coding_rule, anchor_example, color)`.
  `code_key` is a short unique key (e.g. `F1`), `parent_key` is the key of the parent category or `None`, `type` is one of `function`, `structural`, `context`, `crosscutting`, `learning`, `role`, `inductive`, and `color` is a hex colour such as `"#1f77b4"`. The seed is applied only when the project database is empty, i.e. on the very first start. To start over with a changed seed, stop the tool and delete `data/project.db` (this deletes all codings).

Notes for your own categories:

* The analysis views group by category `type`: matrices, interactions and the agency view use the categories of type `function`; the Barriers view uses the category with the key `BAR`. Keep these keys and types, or adapt `app/services/analysis.py`.
* Keyboard shortcuts for coding are mapped to code keys in `SHORTCUTS` in `app/main.py`.
* `FUNCTION_ORDER` in `app/seed.py` sets the order of the function categories in matrices.
* The category order of the readable reports is set in `CATEGORY_ORDER` in `app/services/readable.py`; categories not listed there follow at the end.

**Interview metadata.** The allowed values of the transcript frontmatter are set in
`app/services/importer.py`: `ALLOWED_ROLES` (actor groups), `ALLOWED_LANGUAGES`,
`ALLOWED_MODES` and `ALLOWED_COUNTRIES`. Countries are also fixed in the database schema
(`app/db.py`, `country IN ('DE','CN')`) and in `app/models.py` (`Country`), so a study with
other countries needs these three places changed before the first start.

**Agency values.** The actor types available for agency are defined in `AGENCY_VALUES`
(`app/db.py`) and mirrored in `app/models.py` (`Agency`), `app/services/core.py`
(`AGENCY_MEANINGS`), `static/app.js` (`AGENCIES`, `AGENCY_MEANING`) and
`app/services/readable.py` (`AGENCY_MEANING`). Change all of them together.

**After any code change**, run `python run.py test`. Several tests use the example category
keys and the example values above; adjust them to your own setup.

## Transcript format

One Markdown file per interview, UTF-8 (BOM and CRLF line endings are fine), filename =
interview id. Example (invented):

```markdown
---
id: DE-99
country: DE
role_category: Aggregator, flexibility marketer
role_label: Example respondent at a German flexibility marketer (invented)
date: 2026-07-15
duration_min: 25
language_original: de
interview_mode: video
pilot: true
notes: Anonymised: [Company A] replaces the employer; personal remarks removed, 00:12:00 to 00:12:45.
---

[00:00:03] R: [Greeting omitted.] Sure. I work in product development at [Company A].

[00:00:30] I: Question text …

[00:02:10] R: Answer. [CONFIDENTIAL] excluded part [/CONFIDENTIAL] rest continues. [NO-QUOTE] codable but never quotable [/NO-QUOTE]

[00:02:10] R: Translated answer, [EN-ORIGINAL] this passage was spoken in English in the original interview [/EN-ORIGINAL] and the translation continues here.
```

Rules: the frontmatter keys are `id, country (DE|CN), role_category, role_label, date,
duration_min, language_original (de|en|zh), interview_mode (video|phone|in-person), pilot,
notes` (allowed role categories: Aggregator, flexibility marketer | Grid operator, energy
company | Equipment, storage and EMS manufacturer | Research, consulting | Association, policy |
Other; `notes` is one line that may contain colons and brackets). Paragraphs are separated by
blank lines, are single lines (2000+ characters are normal) and start with `[hh:mm:ss]` and
`I:` or `R:` (the first turn may be `R:`); anything else is refused with the line number. The
**timestamp is kept** per paragraph, shown next to `¶n` in the coding view
(`R ¶12 · 00:22:09`), in the retrieval and search tables and in the exports; two consecutive
paragraphs may share one.

Exactly three tags are interpreted:

* `[CONFIDENTIAL] … [/CONFIDENTIAL]`: not codable, never exported.
* `[EN-ORIGINAL] … [/EN-ORIGINAL]`: spoken in English in the original interview (`EN` marker; exports carry *Original English: yes / partly / no*).
* `[NO-QUOTE] … [/NO-QUOTE]`: quotation block. Coded like everything else (red dotted underline with `NQ` marker), but never a quote candidate and excluded from the quote bank; exports carry *Quotation block: yes / partly / no*.

Tags may overlap each other but must not nest inside themselves. **All other square brackets
are plain, codable text**: anonymisation placeholders (`[Company A]`), editorial notes
(`[Interruption: phone rings.]`, `[inaudible]`).

Respondent turns are numbered `¶1, ¶2, …`; the citation locator everywhere is
`Interview ID, ¶n`, and every segment has a stable reference `<interview id>-S<segment id>`
(first column of `segments.csv` and the quote bank). Duplicates are refused unless
"re-import keeping existing codings" is chosen (codings are re-attached by paragraph index and
text match; failures are listed in the preview; timestamps and tag ranges follow the new file).

## Backups and safety

* `backups/` receives a copy of `data/project.db` on start, on shutdown, every 15 min if something changed (interval configurable), after every import and before every restore or migration.
* Rotation keeps everything from the last 7 days, one per day for 60 days, then one per week, and never fewer than the most recent 20 files.
* Optional secondary backup folder (Settings): a local folder that your own OneDrive/iCloud/Dropbox client syncs. The app itself makes no network calls.
* `PRAGMA integrity_check` runs on every start; a corrupt file is refused and the latest good backup is named (`python run.py restore <file>` or Settings → Restore from backup; a safety copy of the current file is created first).
* All writes are transactional (SQLite WAL, `synchronous=FULL`): killing the process mid-action never loses the previous action.
* The dashboard shows a weekly reminder to export a full dump and copy `backups/` to an external drive.

## Methodological notes (example category system)

* **Category system.** Deductive codes: the seven Bergek et al. (2008) functions with a Knowledge Development / Knowledge Diffusion split (Hekkert et al., 2007), plus Structural Components (actors, networks, institutions), Context Structure (Bergek et al., 2015), Barrier, Function Interaction, Mutual Learning, Role and Emergent Themes. Inductive sub-codes are added during coding (max. three levels) and require a definition and a coding rule; the codebook export flags missing definitions and anchor examples. Internal short ids (F1 … F7) never appear in the UI or exports; only full names do.
* **Valence and agency.** Each code assignment carries +1 / 0 / −1 and an agency: which actor type brought about the situation or could change it (Huang & Malkin, 2026), from the value list `GOV-CENTRAL` (central government / NDRC / NEA / BMWK / legislator), `GOV-PROV` (province, federal state, municipality), `REGULATOR` (BNetzA and similar), `GRID` (grid companies, TSO, DSO), `SOE` (state-owned enterprises other than grid), `MARKET` (private market actors incl. aggregators, banks, customers), `ASSOC` (associations), `RESEARCH` (academia), `MULTI` (explicitly several actor types jointly).
* **Function interactions.** Linked functions get an interaction object (from → to, inducing/blocking, condition) as raw material for cycles (Hekkert & Negro, 2009; Suurs & Hekkert, 2009).
* **Quotation block.** `[NO-QUOTE]` passages are analysed but never quoted: the tool derives a *quotation block* status per segment, refuses the quote-candidate flag on such segments and keeps them out of the quote bank.
* **Pilot phase and changelog.** With the pilot toggle on, every codebook change is logged with `phase = pilot`; the changelog export documents all revisions with date and reason.
* **Intra-coder reliability.** A re-coding pass hides existing codings and writes into a separate pass. The report compares code sets per respondent paragraph: percent agreement overall, and per code the present/absent agreement and Cohen's kappa; disagreements are listed side by side with a resolution note. Inter-coder statistics for several coders are out of scope.
* **Comparison.** Crosstabs and the cross-case view give a two-stage comparison: per country per function, then DE vs. CN along the same categories.

## Project layout

```
run.py                 entry point / CLI
app/                   Python backend (FastAPI + SQLite): database, services, example codebook (seed.py)
static/                vanilla HTML/CSS/JS single-page frontend (no build step, no CDN)
samples/               invented demo transcripts DE-00.md, CN-00.md
tests/                 pytest suite (importer, backup, coding, exports, readable reports, migrations)
tests/fixtures/        DE-77.md: invented real-format fixture (BOM, CRLF, timestamps, placeholders, all three tags)
data/  backups/  exports/   created at runtime, git-ignored
```

## References

* Bergek, A., Hekkert, M., Jacobsson, S., Markard, J., Sandén, B., & Truffer, B. (2015). Technological innovation systems in contexts: Conceptualizing contextual structures and interaction dynamics. *Environmental Innovation and Societal Transitions, 16*, 51–64. https://doi.org/10.1016/j.eist.2015.07.003
* Bergek, A., Jacobsson, S., Carlsson, B., Lindmark, S., & Rickne, A. (2008). Analyzing the functional dynamics of technological innovation systems: A scheme of analysis. *Research Policy, 37*(3), 407–429. https://doi.org/10.1016/j.respol.2007.12.003
* Hekkert, M. P., & Negro, S. O. (2009). Functions of innovation systems as a framework to understand sustainable technological change: Empirical evidence for earlier claims. *Technological Forecasting and Social Change, 76*(4), 584–594. https://doi.org/10.1016/j.techfore.2008.04.013
* Hekkert, M. P., Suurs, R. A. A., Negro, S. O., Kuhlmann, S., & Smits, R. E. H. M. (2007). Functions of innovation systems: A new approach for analysing technological change. *Technological Forecasting and Social Change, 74*(4), 413–432. https://doi.org/10.1016/j.techfore.2006.03.002
* Huang, P., & Malkin, A. (2026). An innovation systems approach to decoding China's technological catch-up: The case of the semiconductor industry. *Industry and Innovation, 33*(4), 431–469. https://doi.org/10.1080/13662716.2025.2463365
* Mayring, P. (2014). *Qualitative content analysis: Theoretical foundation, basic procedures and software solution*. Social Science Open Access Repository (SSOAR). https://nbn-resolving.org/urn:nbn:de:0168-ssoar-395173
* Suurs, R. A. A., & Hekkert, M. P. (2009). Cumulative causation in the formation of a technological innovation system: The case of biofuels in the Netherlands. *Technological Forecasting and Social Change, 76*(8), 1003–1020. https://doi.org/10.1016/j.techfore.2009.03.002
