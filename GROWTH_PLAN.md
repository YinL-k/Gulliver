# Gulliver growth plan (0.3.0 launch)

> **Status update (2026-11-02, on the PR #17 branch):**
> - **A0 is fixed.** The self-join rehearsal now samples only one occurrence of a table that is read twice, and uses
>   bernoulli sampling on DuckDB. On the demo, five seeds predict 252M-257M rows; the real result is 254,966,150 rows.
>   The README quick-start numbers and the site ladder are updated with measured values.
> - **0.3.0 docs and version are in:** the README, AGENTS.md, the skill, llms.txt, the site and all package files.
> - **Still for the human:** re-record `docs/demo.svg` (`docs/demo.sh` needs asciinema and svg-term) and
>   `docs/og.png`; both still show the old prediction. Then everything from section 3 (social preview, Discussions,
>   publishing to PyPI/npm/MCP Registry, filing the good first issues).

Written 2026-11-02. Time zone: Europe/Amsterdam (CET, UTC+1). US is on standard time since 1 Nov, so 15:00 Amsterdam = 09:00 ET = 06:00 PT.
Repo: https://github.com/laaielichter/Gulliver. Nothing in this plan has been posted, filed or submitted.

Rules for everything below (from the skill's `ethics-guardrails.md`):
- Say "I built this" / "I'm the author" in every post, PR and form.
- Every post names one honest limitation and asks one specific question.
- Never ask for stars, upvotes or comments. No friends-upvote messages, no alt accounts, no giveaways.
- Different text for every subreddit. No identical cross-posts, no delete-and-repost.
- Hacker News: the human writes the title, post and every comment. This plan only gives notes and facts.
- No faked demo, numbers or downloads. Every number in a post must come from a real run.

---

## 0. Finding from today that changes the launch (read first)

I re-ran the README demo on 2026-11-02 (`gulliver demo`, 5,000,000 events, 100,000 users, Gulliver 0.2.0 from the repo venv):

| Run | 1% rows | 3% rows | 10% rows | Predicted full run (range) | Confidence shown |
|---|---|---|---|---|---|
| README (recorded) | 41,706 | 237,422 | 2,438,395 | 134,525,654 (67.3M - 269.1M) | not shown |
| Today, run 1 | n/a | n/a | n/a | 66,283,305 (33.1M - 132.6M), exponent 1.48 | n/a |
| Today, run 2 | 102,589 | 266,249 | 2,564,545 | 54,234,501 (27.1M - 108.5M) | medium |
| Today, run 3 | 73,370 | 247,742 | 2,658,534 | 83,533,837 (41.8M - 167.1M) | medium |

**The real full-run size**, computed exactly with `select sum(n*n) from (select user_id, count(*) n from events group by 1)`: **254,966,150 rows.**

What this means:
- The warning works. Every run flagged "grows faster than the data" (exponent 1.48-1.77, above the 1.3 threshold).
- The number is too low by 2-5x. In all three of today's runs the true value was above the top of the range, while the confidence said `medium`.
- The cause is in the code's own note, "a join where both sides were sampled". When both sides of a self-join are sampled at fraction p, the output scales with p², so the full run is 10% rows x 100 = 2,564,545 x 100 ≈ 256M. That is within 1% of the true number.
- Hacker News readers will run `gulliver demo` and compare. The README claim "warns that the full result has more than 100 million rows" did not hold in 3 of 3 runs today.

**Before launch (Claude can do this on the PR #17 branch, with your go-ahead):** when the same table is sampled on both sides of a join, either sample only one side, or fit with the known exponent 2 and say so in `notes`. Also lower the confidence to `low` when the exponent is above 1.3 and both join sides are sampled. Then re-record `docs/demo.svg` and update the README numbers to the new real output.

**Use it in the launch.** This makes a better story than a perfect number: "my predictor was off by 4x on its own demo, here is why (p² sampling) and how I fixed it". It fits the dev.to article and the r/dataengineering post below.

---

## 1. Audit score and prioritized fixes

**Score: 33/44.** The page is in good shape. The three main gaps:
1. Nothing about 0.3.0 shows on the README, site or AGENTS.md.
2. No social preview and no Discussions.
3. The good-first-issue list goes to 0 when PR #17 merges.

### A. Changes Claude can make on the PR #17 branch (each needs your go-ahead; nothing is edited yet)

| # | Fix | Why |
|---|---|---|
| A0 | Fix the self-join prediction (section 0), then re-record the demo and update the README numbers | The demo is the first thing launch visitors run |
| A1 | README for 0.3.0: the callout (read-only gate with the 132-case bypass corpus, CI lint with SARIF, `gulliver review` PR gate); one section per feature linking `docs/features/*.md`; lint table with GV014 and GV015 and "15 rules"; the **breaking** DuckDB sandbox change (`allowed_paths` / `allow_remote`) in the callout | 0 hits today for `review`, `selftest`, `top`, `--verify`, SARIF, elicitation, `--toolset`, pre-commit, GV014, GV015 |
| A2 | Bump to 0.3.0 in `pyproject.toml`, `src/gulliver/__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `server.json` (3 places), `npm/package.json`, `docs/index.html`, the README callout, plus a `## 0.3.0 - 2026-11-09` CHANGELOG section | `release.yml` checks that the tag matches the version |
| A3 | Rewrite "Why" as outcomes. For example: the agent cannot run a query over budget; you see the full-run size before it runs; slow SQL gets a rewrite with a stated rows guarantee; a PR shows its cost change and what it breaks downstream; agents cannot escape read-only. Keep the step table below it | Audit item 17 |
| A4 | Second comparison table (CI/PR: SQLFluff, Squawk, dbt-costgate, Recce, Datafold, dbt Advanced CI) from `synth.md` section B, updated for 0.3.0. Keep "where Gulliver is behind": no index or health tuning (Postgres MCP Pro is stronger), no Databricks or ClickHouse, BigQuery and Snowflake tested only against fakes, a tool schema larger than DBHub's (smaller with `--toolset`). Date it November 2026 | Audit item 19 |
| A5 | Site `docs/index.html`: a 0.3.0 news line, 15 rules, cards for the security gate and the PR gate. Sync `llms.txt` and AGENTS.md (16 tools including `gulliver_top`; check the count in `mcp_server.py`) | The site says "New in 0.2.0" |
| A6 | File the 5 new good first issues (section 4) **before** PR #17 merges | The badge and the Contributing link would show 0 |
| A7 | After Discussions is on: a `contact_links` entry "Question / show your case -> Discussions" in `.github/ISSUE_TEMPLATE/config.yml`, a Discussions link in the README "Roadmap, contributing, community" section, and fix `gulliver` -> `Gulliver` in the `config.yml` URL | Audit items 12 and 14 |
| A8 | Draft the v0.3.0 GitHub release notes from the PR #17 body and the CHANGELOG, with a "Breaking" heading | Release page is a backlink and the "what's new" link for posts |
| A9 | Add an `npx skills add laaielichter/gulliver` line to the README install section | skills.sh lists by real installs; there is no form |
| A10 | Add the Glama score badge to the README once Glama shows a score | awesome-mcp-servers checks for it |

### B. Things only you can do: see section 3.

---

## 2. Readiness gate (do not schedule the launch until every line is "yes")

| Gate item | State on 2026-11-02 | Needed | Owner |
|---|---|---|---|
| Real demo (GIF/video) | **Partly.** `docs/demo.svg` is a real CLI recording, but its numbers no longer reproduce (section 0) and it shows no agent | (1) Re-recorded CLI demo after fix A0. (2) A 10-30 s screen recording of a real Claude Code session: prompt, `gulliver_preflight` -> `needs_rehearsal`, `gulliver_rehearse` warning, `autofix`. (3) Screenshot of a real `gulliver review` sticky comment on a test PR | You record, Claude fixes A0 |
| Copy-paste quickstart with expected output | **Yes**, `pip install gulliver-mcp` + `gulliver demo`. The output table needs updating after A0 | Updated numbers | Claude |
| Social preview | **No** (generic GitHub card). `docs/og.png` is ready, 1280x640, 87 KB | Upload it | You |
| 0.3.0 on GitHub Release | **No.** Latest is v0.2.0. PR #17 is a draft, `mergeable_state: unstable` | Merge, tag `v0.3.0`, publish release notes | You (Claude drafts notes) |
| 0.3.0 on PyPI | **No**, latest 0.2.0 | `release.yml` publishes on tag (trusted publisher) | You (tag) |
| 0.3.0 on npm | **No**, latest 0.2.0 | `cd npm && npm publish` (manual) | You |
| MCP Registry | **Not listed** (search returns 0) | `mcp-publisher publish` with `server.json` at 0.3.0 | You |
| 3-5 good first issues | **5 today, 0 after the merge** | File the 5 in section 4 first | Claude after your yes |
| Discussions on, with a pinned thread | **Off** | Turn on, pin "Show your agent-SQL horror story" | You |
| Human online 6-8 h on launch day | **Unknown** | Block Tue 2026-11-17 13:30-22:00 in your calendar | You |
| One outsider tried the README | **Not done** | Ask one person who has never seen it to run the quickstart, and watch where they get stuck | You |

---

## 3. Manual to-dos for you (in this order)

**This week (Nov 2-8)**
1. Say yes or no to fix A0 (the self-join prediction) and to A1-A10, so Claude can work on the PR #17 branch.
2. Upload the social preview: repo Settings > General > Social preview > `docs/og.png`. There is no API for this.
3. Turn on Discussions: Settings > General > Features. Create and pin a thread "Show your agent-SQL horror story" (what an agent ran, what it cost or broke).
4. Topics: use the gear next to About. They are at the cap of 20. Swap: remove `data-lineage` and `schema-drift`, add `github-actions` and `ai-safety`. The resulting set passes `check_topics.py`.
5. Optionally shorten the About description to: "MCP server + CLI: a dress rehearsal for AI agents' SQL. Cost preflight, scale lint with same-rows autofix, 1%→10% rehearsal, budget gate, read-only security gate, PR cost review. DuckDB, Postgres, BigQuery, Snowflake."
6. Glama: sign in with GitHub and claim https://glama.ai/mcp/servers/laaielichter/Gulliver, so you can trigger a rebuild. It still shows v0.1.0 with 13 tools.

**Release day (Mon 2026-11-09)**
7. Mark PR #17 ready, check CI is green, merge, tag `v0.3.0`, publish the release with the drafted notes.
8. Check https://pypi.org/project/gulliver-mcp/ shows 0.3.0 with all 5 project URLs.
9. `cd npm && npm publish`, then `npm deprecate gulliver-mcp@0.0.0-stage "test publish, use 0.3.0+"`.
10. `mcp-publisher login github` then `mcp-publisher publish`. Check with `curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.laaielichter/gulliver"`.
11. Check the Claude Code plugin marketplace entry shows 0.3.0 (`/plugin marketplace update gulliver`).

**Week before launch (Nov 9-15)**
12. Record the two demo assets (gate table, row 1). Use a real session; do not edit numbers.
13. Ask one outsider to try the README (gate table, last row).
14. Take part in r/mcp, r/ClaudeCode and r/dataengineering without links: answer 5-10 questions where you know the answer. This builds the history the 1-in-10 self-promotion rule expects.
15. Read the sidebar rules of every subreddit in section 6 and note anything that differs from this plan. None of the Reddit rules could be fetched on 2026-11-02 (403).
16. Write your Show HN title and first comment in your own words from the notes in section 6.1.

**Launch day (Tue 2026-11-17)**: see the calendar in section 7. Be online 13:30-22:00.

---

## 4. Five new good first issues (ready to paste)

Labels on each: `good first issue`, `enhancement`. File these before PR #17 merges. The file paths refer to the PR #17 branch, so check the line numbers again after the merge.

### Issue 1

**Title:** `gulliver rules --format markdown|json, and generate the README lint table from it`

```markdown
The README lint table in "Scale lint rules" stops at GV013, while the code has 15 rules (GV014, GV015 are missing).
A generator stops this drift.

**Where to start**
- `src/gulliver/cli.py:218`: the `rules` parser has no `--format`; `cli_rules` only knows `--json`
- `src/gulliver/lint_report.py:416`: `cli_rules` / `render_rules_text`
- `tests/test_lint_report.py`

**Done when**
- [ ] `gulliver rules --format markdown` prints a `| Rule | Name | Severity |` table for every rule in `lint.RULES`
- [ ] `--format json` gives the same data as `--json` today (keep `--json` working)
- [ ] A test checks that the README lint table matches the generated table
- [ ] The README table is regenerated
```

### Issue 2

**Title:** `gulliver spend -o PATH and gulliver top -o PATH`

```markdown
`receipt -o`, `translate -o` and `fix -o` already write to a file (#6, #11, #12). `spend` and `top` only print,
so a CSV for a FinOps sheet has to be piped by hand.

**Where to start**
- `src/gulliver/cli.py:325-343`: the `spend` and `top` parsers
- `src/gulliver/cli.py:506-514`: where their output is printed
- `src/gulliver/runaway.py:317`: `render_spend`
- `tests/test_runaway.py`

**Done when**
- [ ] `gulliver spend --format csv -o spend.csv` writes exactly the text it would print, as UTF-8
- [ ] The same works for `gulliver top -o top.md`
- [ ] It prints a one-line confirmation with the path
- [ ] A test covers both commands
```

### Issue 3

**Title:** `Lint rule GV016 order-by-random`

```markdown
Agents write `ORDER BY RANDOM() LIMIT 100` to sample rows. That sorts the whole table. `TABLESAMPLE` (Postgres,
BigQuery, Snowflake) or `USING SAMPLE` (DuckDB) reads far less.
sqlglot already parses `exp.Rand` (it is used in `src/gulliver/runaway.py:48`).

**Where to start**
- `src/gulliver/lint.py`: the `RULES` dict (around line 20) and a check in the walker
- `src/gulliver/lint_report.py`: rule metadata
- `tests/test_lint.py`
- README lint table

**Done when**
- [ ] `ORDER BY RANDOM()` and `ORDER BY RAND()` give a `warn` that suggests `TABLESAMPLE` / `USING SAMPLE`
- [ ] `RANDOM()` in the SELECT list without `ORDER BY` gives no finding
- [ ] Tests for the DuckDB, Postgres and BigQuery dialects
- [ ] The rule appears in `gulliver rules` and the README table
```

### Issue 4

**Title:** `Result guard: detect UK National Insurance numbers by value`

```markdown
`category_from_name` already maps a column called `nino` to `ssn`, but a column called `contact_ref` that holds
NINOs is not tagged, because `category_from_value` has no NINO pattern.

**Where to start**
- `src/gulliver/guard.py`: `category_from_name` (around line 108), `category_from_value` (around line 202)
- `tests/test_guard.py`

**Done when**
- [ ] A `nino_valid()` helper matches `^[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]$` after removing spaces, and rejects the
      prefixes BG, GB, NK, KN, TN, NT and ZZ
- [ ] A column of NINOs (60% or more of its text cells) is tagged `ssn`
- [ ] Random 9-character codes are not tagged
- [ ] Positive and negative tests
```

### Issue 5

**Title:** `Prompt-injection scan: non-English "ignore previous instructions"`

```markdown
The `override` pattern in `INJECTION_PATTERNS` only knows English verbs, so rows in Dutch, German, French or
Spanish that tell the agent to ignore its instructions are not flagged.

**Where to start**
- `src/gulliver/guard.py:249`: `INJECTION_PATTERNS`
- `tests/test_guard.py`

**Done when**
- [ ] A new `override_i18n` pattern flags phrases such as "negeer alle vorige instructies",
      "ignoriere alle vorherigen Anweisungen", "ignore toutes les instructions précédentes" and
      "ignora todas las instrucciones anteriores"
- [ ] Normal sentences such as "negeer de eerste rij" are not flagged
- [ ] One positive and one negative test per language
```

---

## 5. Listings

You submit each one yourself and say "I'm the author". One PR or form per list, only where Gulliver is on topic.

| # | Channel | State (2026-11-02) | Earliest date | Steps | Entry |
|---|---|---|---|---|---|
| 1 | Official MCP Registry | Not listed | 2026-11-09 (after npm + PyPI 0.3.0) | `mcp-publisher login github`, `mcp-publisher publish`, check with the curl in to-do 10 | `server.json` as is (bump the 3 version fields) |
| 2 | Glama | Listed, no score yet, shows v0.1.0 | Now | Claim, trigger rebuild after 0.3.0. If asked for a build spec: `uvx gulliver-mcp` in a Python image. Improve the descriptions of low-scored tools (`gulliver_drift` B 3.3) | Auto from repo |
| 3 | punkpeye/awesome-mcp-servers | Not listed | When Glama shows a score | Edit `README.md` only. Section `### 🗄️ Databases`, directly before `- [lagaam-ai/lagaam]`. PR title `Add laaielichter/Gulliver`, body "I'm the author" + Glama link. You open it yourself, so do not add the `🤖🤖🤖` agent marker | Entry A below |
| 4 | Claude plugin directory | Not confirmed | 2026-11-10 | `claude plugin validate .`, then https://claude.ai/directory/manage > Submit new > Plugin bundle. Repo `laaielichter/Gulliver`, path `.`, ref `v0.3.0`. Data handling: queries go to the database the user configures; receipts stored locally; not aimed at under-18s. Keep "GitHub push webhook" updates | From `plugin.json` + README |
| 5 | hesreallyhim/awesome-claude-code | Not listed | **2026-11-16** (14 days old + active commits) | Web issue form only: https://github.com/hesreallyhim/awesome-claude-code/issues/new?template=recommend-resource.yml. No PR, no CLI, you fill it in by hand | Entry B below |
| 6 | Hiflylabs/awesome-dbt | Not listed | 2026-11-23 | `### Agents, MCP & Skills`, **first** entry, above `schemagate`. Or the issue form `/issues/new?template=suggest-resource.yml`. Disclose authorship | Entry C below |
| 7 | davidgasquez/awesome-duckdb | Not listed | 2026-11-24 | `### Data APIs, Integration & AI Access` > `#### Free & Open Source`, appended after `schemagate`. PR title `Add Gulliver`, body "I'm the author of Gulliver." | Entry D below |
| 8 | mcp.so | Not listed | 2026-11-12 | https://mcp.so/submit, type "MCP Server". Use the free reviewed path, not the $39 option | URL `https://github.com/laaielichter/Gulliver`, Name `Gulliver` |
| 9 | mcpservers.org | Could not verify (403) | 2026-11-12 | Search the site by hand first, then https://mcpservers.org/submit, free tier | Entry E below |
| 10 | PulseMCP | Not listed, submissions paused | Check 2026-11-30 | If https://pulsemcp.com/submit is open, submit; it may also pick up the MCP Registry entry (not verified) | Entry E short description |
| 11 | skills.sh | Not listed | After A9 | No form. Add the `npx skills add laaielichter/gulliver` README line. Do not run fake installs | n/a |
| 12 | Smithery | Could not verify | Optional, week 5 | Build an `.mcpb` bundle that runs `uvx gulliver-mcp`, then `npx @smithery/cli mcp publish ... --transport stdio`. Check `smithery --help` first | `server.json` description |
| 13 | igorbarinov/awesome-data-engineering | Not listed | **2026-12-01**, and only with a sign of outside use (an outside issue, PR or star) | `## Testing`, at the bottom, after `dbmask`. One PR | Entry F below |
| 14 | dhamaniasad/awesome-postgres | Not listed | Not before real outside adoption ("not newly created") | `### Optimization`, end of list | Entry G below |
| 15 | coty/awesome-bigquery | Not listed, last push 2020 | Skip | n/a | n/a |

**Entry A (awesome-mcp-servers):**
```md
- [laaielichter/Gulliver](https://github.com/laaielichter/Gulliver) [![laaielichter/Gulliver MCP server](https://glama.ai/mcp/servers/laaielichter/Gulliver/badges/score.svg)](https://glama.ai/mcp/servers/laaielichter/Gulliver) 🐍 🏠 🍎 🪟 🐧 - Dress rehearsal for agent-written SQL on DuckDB, Postgres, BigQuery and Snowflake: cost preflight, scale lint with same-rows autofix, rehearsal on 1%/3%/10% samples to predict the full run, and budget-gated execution with human approval and receipts. `uvx gulliver-mcp`
```

**Entry B (awesome-claude-code form):**
- Name: `Gulliver`
- URL: `https://github.com/laaielichter/Gulliver`
- Category: Plugins (or Agent Skills, whichever the form offers)
- Description: `Claude Code plugin (MCP server + skill) that makes Claude check SQL before running it on big tables: cost preflight, scale lint with same-rows autofix, rehearsal on 1%/3%/10% samples to predict the full run, and budget-gated execution with human approval and receipts. DuckDB, Postgres, BigQuery, Snowflake.`
- Author: `laaielichter. I am the author.`

**Entry C (awesome-dbt):**
```md
- [Gulliver](https://github.com/laaielichter/Gulliver) - MCP server, CLI and agent skill that shows which dbt models and columns a column change breaks (column-level lineage from SQL models or a dbt manifest), reports schema drift, proposes incremental or microbatch configs with the rows saved, and preflights model SQL for cost before an agent runs it.
```
After 0.3.0 you may add before the final period: `, with a PR gate (gulliver review) that reports cost change and breaking columns per pull request`.

**Entry D (awesome-duckdb):**
```md
- [Gulliver](https://github.com/laaielichter/Gulliver) - MCP server and CLI that checks agent-written SQL before a full run: cost preflight, scale lint with same-rows rewrites, and rehearsal on 1%/3%/10% samples to predict the full result, with DuckDB as the default local engine (also Postgres, BigQuery, Snowflake). MIT.
```

**Entry E (mcpservers.org):**
- Server Name: `Gulliver`
- Category: `Database`
- Short Description: `Dress rehearsal for agent SQL: cost preflight, scale lint with same-rows autofix, 1%/3%/10% sample rehearsal that predicts the full run, and budget-gated execution with receipts. DuckDB, Postgres, BigQuery, Snowflake.`
- Repository: `https://github.com/laaielichter/Gulliver`
- Official MCP Registry Name: `io.github.laaielichter/gulliver`
- Remote connections: unchecked (stdio, runs locally)
- Contact email: your own address

**Entry F (awesome-data-engineering, from 2026-12-01):**
```md
- [Gulliver](https://github.com/laaielichter/Gulliver) - MCP server and CLI that estimates the cost of a SQL query, rehearses it on 1%, 3% and 10% samples to predict the full run, and runs it only within a budget, for DuckDB, Postgres, BigQuery and Snowflake.
```

**Entry G (awesome-postgres, later):**
```md
* [Gulliver](https://github.com/laaielichter/Gulliver) - estimates query cost with EXPLAIN, rehearses SQL on table samples to predict the full run, and blocks writes and unsafe functions before an AI agent runs a query.
```

---

## 6. Launch drafts per platform

Facts every draft may use (all checked in the repo on 2026-11-02):
- `pip install gulliver-mcp` then `gulliver demo` makes a DuckDB file with 5,000,000 events and 100,000 users. No sign-up, no warehouse.
- The self-join `select a.event_id, b.event_id from events a join events b on a.user_id = b.user_id` returns 254,966,150 rows on that file.
- Rehearsal runs on 1%, 3% and 10% samples and fits a growth exponent. Above 1.3 it warns "grows faster than the data".
- Read-only gate codes GS001-GS004 (server control, session state, dynamic SQL, file access). `gulliver selftest security` reports "bypass test set: 132/132 cases as expected".
- 15 scale lint rules (GV001-GV015). `gulliver fix` labels every rewrite `same`, `assumes`, `nulls` or `changes`; `--verify` tests rewrites on generated edge-case tables.
- `gulliver review` reports per changed model: new lint, change type (`cosmetic` / `non_breaking` / `breaking`), downstream impact, cost before/after per month, and a `superlinear` flag with `--rehearse`.
- Engines: DuckDB and Postgres are tested in CI. BigQuery and Snowflake are beta, tested against fakes only.
- MIT. MCP server (stdio or Streamable HTTP) and CLI. Claude Code plugin through `/plugin marketplace add laaielichter/gulliver`.

Do **not** use: any BigQuery or Snowflake dollar figure (none was measured on a real warehouse), prediction accuracy claims, or user counts.

Honest limitations to pick from (one per post):
- Predictions on joins that multiply rows are rough. On its own demo the 0.2.0 predictor was 2-5x too low (section 0). Say what was fixed if A0 ships.
- BigQuery and Snowflake are tested only against fakes.
- No index or health tuning; Postgres MCP Pro does that better.
- No Databricks or ClickHouse.
- DuckDB and Postgres cost estimates are labeled `heuristic`; only BigQuery's dry-run is `exact`.
- The tool schema is bigger than DBHub's; `--toolset` reduces it.

### 6.1 Show HN

> **HUMAN MUST REWRITE IN OWN WORDS. These are notes and facts only, not final text.**
> HN asks people not to post generated or AI-edited text. Write the title, the first comment and every reply yourself.

- **When:** Tue 2026-11-17, 14:00 Amsterdam (08:00 ET). Be online until 22:00.
- **Eligibility:** no earlier Show HN for Gulliver was found on HN (Algolia search, 2026-11-02). Your account `laaielichter` has comments from 2026-11-02, so it is not brand new. This is a first Show HN, not a point-release repost.
- **Title shape:** `Show HN: Gulliver – <plain description in your words>`. No capitals for emphasis, no exclamation marks, no "revolutionary". Ideas for what the description should say: rehearses agent SQL on 1/3/10% samples before the full run; or a cost and safety gate for SQL written by AI agents.
- **Link:** the GitHub repo (not the site), so the quickstart is one scroll away.
- **First-comment outline (facts for you to put in your own words):**
  1. Why: one real moment where an agent query surprised you (only if it happened; otherwise the general problem: an agent does not know `events` is huge or that a join multiplies rows).
  2. What it does: preflight (bytes, rows, money), 15 scale lint rules with rewrites labeled by rows guarantee, rehearsal on 1/3/10% samples, budget gate with human approval, receipts.
  3. How the prediction works: log-log fit of output rows and runtime against the sample fraction; exponent near 1 = linear, above 1.3 = warning.
  4. What was hard: the p² effect when both sides of a self-join are sampled, and what you changed (section 0).
  5. 0.3.0: read-only gate beyond "only SELECT": `pg_terminate_backend`, `set_config(... read_only off)`, `dblink`, `pg_read_file`, DuckDB `read_csv('~/.aws/credentials')`. 132-case bypass corpus in the repo.
  6. What is rough: BigQuery and Snowflake only tested against fakes; no Databricks or ClickHouse.
  7. Try it: `pip install gulliver-mcp && gulliver demo`, no sign-up.
  8. One question, for example: "If you run agents against a warehouse, what was the query that hurt?" or "Would you trust a 3-point sample fit, or should it refuse to give a number above exponent 1.3?"
- **Hard questions to prepare answers for (in your own words):**
  - How is this better than `EXPLAIN` or BigQuery dry-run? (Those give bytes scanned, not output rows of a fan-out join, and do not gate or rewrite.)
  - Sampling is biased for skewed keys. How wrong can it be? (Give the real demo numbers.)
  - Why not a read-only database role? (Agree it is the strongest layer; the gate is defense in depth and the README should say to use both. Docs PR #8 adds a Postgres read-only role example.)
  - Was this written with AI? Answer honestly.
- Do not tell anyone the link to "go upvote". Sharing the post in your own channels as a normal update is fine; asking for votes is not.

### 6.2 Reddit (different text per sub; read each sidebar on the day; all rules UNVERIFIED)

Only post from an account with history in that sub. Use the showcase/flair the sub asks for. One sub per day at most.

#### r/mcp (Wed 2026-11-18, 15:30)
**Title:** `I built an MCP server that rehearses agent SQL on 1%/3%/10% samples before the full run (open source, 0.3.0)`

> Agents that query a database do not know how big the tables are. A self-join on a non-unique key looks fine on a
> test table and returns 255 million rows on a 5-million-row one.
>
> Gulliver is an MCP server (stdio or Streamable HTTP) that sits between the agent and DuckDB, Postgres, BigQuery
> or Snowflake. Every tool answer has a `status` and a `next_step` (`ok`, `needs_rehearsal`, `needs_approval`,
> `block`), so smaller models know what to do next.
>
> MCP-specific parts in 0.3.0:
> - Approval through elicitation: when a run costs more than the auto-approve limit, the client shows the human a
>   form. Opt-in, and only the human can turn it on in `gulliver.toml`.
> - Resources and prompts, so a client can attach table context and start the loop without a tool call.
> - `--toolset` to load fewer tool schemas.
>
> [demo video]
>
> Limitation: the tool schema is still larger than DBHub's, even with `--toolset`.
>
> Question: for approval, would you rather get an elicitation form in the client, or keep it as a separate CLI
> command so the agent can never reach it?
>
> Disclosure: I'm the author. MIT. `uvx gulliver-mcp` / https://github.com/laaielichter/Gulliver

#### r/ClaudeCode (Thu 2026-11-19, 15:30)
**Title:** `I made a Claude Code plugin that makes Claude preflight and rehearse SQL before it hits a big table`

> When Claude Code writes SQL against a real warehouse, it has no idea that `events` has billions of rows. I built
> a plugin (MCP server + skill) that gives it a fixed loop: inspect the tables, preflight the cost, rehearse on
> 1%, 3% and 10% samples, then run inside a budget. Over the limit, it stops and gives me a receipt id to approve.
>
> Install:
> `/plugin marketplace add laaielichter/gulliver`
> `/plugin install gulliver@gulliver`
>
> [screen recording: the prompt, gulliver_preflight -> needs_rehearsal, the rehearse warning, the autofix]
>
> The skill tells Claude to use `autofix.fixed_sql` only when the rewrite is labeled `same` (same rows). Claude
> cannot approve its own runs; approval is a command I run.
>
> Limitation: BigQuery and Snowflake support is tested only against fakes so far. DuckDB and Postgres are tested in CI.
>
> Question: does the skill make Claude call the tools at the right moments for you, or does it over-call inspect?
>
> I'm the author; it is MIT and free.

#### r/ClaudeAI (week 1: post in the "Built with Claude" showcase megathread unless the sidebar allows a flair post)
**Comment text:**

> **Gulliver**: a Claude Code plugin / MCP server that makes Claude test SQL on 1%, 3% and 10% samples before it
> runs on the full table. How Claude was used: I built it with Claude Code, and Claude is also the main user: the
> skill teaches it the inspect → preflight → rehearse → run loop. Free and open source (MIT).
> One thing it does not do yet: Databricks or ClickHouse.
> Question for people who let Claude query production data: what do you use today to stop a runaway query?
> I'm the author. https://github.com/laaielichter/Gulliver

(Change "I built it with Claude Code" to what is true for you.)

#### r/dataengineering (Tue 2026-11-24, 15:30; or the monthly discussion thread if you lack sub history)
**Title:** `Predicting the full-run size of a query from 1%, 3% and 10% samples: what my predictor got wrong on a self-join`

> I am building an open-source tool that lets AI agents rehearse SQL on table samples before running it. The idea:
> run on 1%, 3%, 10%, fit output rows against the sample fraction on a log-log scale, and extrapolate to 100%.
> Exponent near 1 means it grows with the data; above 1.3 means a join is multiplying rows.
>
> On my own demo (5M events, 100k users, a self-join on `user_id`) the true output is 254,966,150 rows. Three
> rehearsals predicted 54M, 66M and 84M, labeled `medium` confidence. The warning fired every time, but the
> number was 3-5x too low.
>
> Reason: both sides of the join are sampled, so the output scales with p², not with p. 10% of both sides gives
> 1% of the pairs. 2.56M rows at 10% x 100 ≈ 256M, which is right.
> [what you changed, once A0 ships]
>
> Limitation that remains: skewed keys. If one user has most of the events, a 1% sample may miss them.
>
> Question: has anyone done sample-based cost prediction in production (Snowflake, BigQuery, Spark)? How do you
> handle skew: stratified samples, or sampling only one join side?
>
> Disclosure: I'm the author of Gulliver (MIT): https://github.com/laaielichter/Gulliver

#### r/duckdb (Thu 2026-11-26, 15:30)
**Title:** `I built a read-only gate for agents on DuckDB: it blocks read_csv('~/.aws/credentials'), getenv() and paths outside allowed folders`

> If you give an AI agent a DuckDB connection, "only SELECT" is not read-only. `select * from read_csv('~/.aws/credentials')`
> is a SELECT. So are `getenv('HOME')`, `read_text`, `http_get` and `query('...')` with SQL in a string.
>
> Gulliver 0.3.0 checks every statement before DuckDB sees it: file readers are limited to `allowed_paths`, hidden
> files (`.env`, `.gulliver/`, also as globs like `[.]env` or `**/credentials`), `../`, computed paths and symlinks
> that lead out are refused, and globs are expanded to check what they match right now. `gulliver selftest security`
> runs a 132-case bypass corpus.
>
> It also rehearses queries with `USING SAMPLE` on 1/3/10% to predict the full run.
>
> This is a breaking change from 0.2.0: queries that read files outside the DuckDB file's folder and the current
> folder now need `allowed_paths` (or `allow_remote` for URLs).
>
> Limitation: it is a parser-level gate. If you can run DuckDB with `enable_external_access=false` you should do that too.
>
> Question: which DuckDB functions or extensions would you add to the file-access list?
>
> I'm the author; MIT. https://github.com/laaielichter/Gulliver

(Check first whether Gulliver uses DuckDB's own `enable_external_access`/`allowed_directories` settings and change the limitation line to match.)

#### r/PostgreSQL (Wed 2026-12-02, 15:30)
**Title:** `A SELECT can still call pg_terminate_backend: the read-only checks I added for AI agents on Postgres`

> Most database MCP servers check the statement type. These are all single SELECTs:
> - `select pg_terminate_backend(pid) from pg_stat_activity`
> - `select set_config('default_transaction_read_only', 'off', false)`
> - `select * from dblink('...', 'delete from ...') as t(x int)`
> - `select pg_read_file('/etc/passwd')`
> - `select nextval('orders_id_seq')`, `select ... for update`
>
> Gulliver refuses them before they reach the server, with codes GS001-GS004 (server control, session state,
> dynamic SQL, file access), matched in CTEs, subqueries, LATERAL and UNION branches, after removing quotes,
> schemas and `U&"..."` escapes. It also runs in a read-only session. The repo has a 132-case bypass corpus.
>
> Limitation: a Postgres role with only SELECT grants is still the strongest layer. Use both.
>
> Question: what is missing from the list? I am especially unsure about extension functions (PostGIS, pg_cron, etc.).
>
> I'm the author; MIT. https://github.com/laaielichter/Gulliver

#### r/bigquery (only if you have a real BigQuery run to show; target Tue 2026-12-08, 15:30)
**Title:** `I built a budget gate for agent-written BigQuery SQL: dry-run bytes, then rehearsal on samples, then a $ limit`

> BigQuery dry-run tells you bytes scanned. It does not tell you that a join returns 50x more rows than the input,
> and it does not stop the query. Gulliver uses the dry-run as an `exact` byte estimate, adds scale lint
> (e.g. `DATE(ts) = ...` blocks partition pruning, with a same-rows rewrite), rehearses on `TABLESAMPLE` 1/3/10%,
> and runs only under per-query and per-day $ limits. `gulliver top` reads `INFORMATION_SCHEMA.JOBS_BY_PROJECT`
> and ranks repeated queries with a priced rewrite.
>
> [one real example from your own project: bytes before and after a rewrite. Do not post without it.]
>
> Limitation: BigQuery support is beta; so far it has been tested against a fake client, not a live project.
> I'd like testers.
>
> Question: what would you need to see before letting an agent query a production project through it?
>
> I'm the author; MIT.

#### r/snowflake (only with a real Snowflake run; target Thu 2026-12-10, 15:30)
**Title:** `Open-source pre-run check for agent SQL on Snowflake: upper-bound scan estimate, sample rehearsal, credit budget`

> Gulliver estimates scan size from Snowflake metadata (labeled `upper_bound`, not exact), rehearses on
> `SAMPLE` 1/3/10%, and blocks `SYSTEM$CANCEL_QUERY`, `SYSTEM$ABORT_SESSION` and other `SYSTEM$` functions not known
> to be read-only. `gulliver top` reads `ACCOUNT_USAGE.QUERY_HISTORY` (with `QUERY_ATTRIBUTION_HISTORY` when the role
> can) and ranks repeated queries by credits.
>
> [one real example]
>
> Limitation: tested against fakes only so far; when attribution history is not readable, cost is execution time x
> warehouse size, which is a heuristic.
>
> Question: which role and warehouse setup would you use to try a tool like this safely?
>
> I'm the author; MIT.

#### r/LocalLLaMA: skip (off topic, 1/10 self-promotion rule).

### 6.3 dev.to article (Wed 2026-11-18, 09:30; written by you, with AI-assistance disclosed if used)

**Title:** `How I stopped my AI agent from running a 255-million-row self-join`

**Outline:**
1. The query. `select a.event_id, b.event_id from events a join events b on a.user_id = b.user_id` on a 5M-row table. It is valid SQL and it returns 254,966,150 rows. An agent writing it has no way to know.
2. Why `EXPLAIN` and dry-runs are not enough. They estimate scan size, not fan-out, and they do not stop anything.
3. The idea: a dress rehearsal. Run on 1%, 3% and 10% samples (`USING SAMPLE` / `TABLESAMPLE`), measure output rows and runtime, fit `log(rows) = a + k·log(fraction)`.
4. Reading the exponent. Aggregations: k near 0. Filters and scans: k near 1. Fan-out joins: k above 1.3, which triggers the warning.
5. The bug I found in my own demo. Real numbers from section 0: predicted 54M-84M, true 255M. Why: both sides sampled means p². The fix (from A0).
6. What the agent sees. A JSON excerpt from `gulliver_rehearse`: `status`, `prediction.output_rows.value/low/high`, `exponent`, `confidence`, `next_step`. Use real output.
7. The rest of the gate in one paragraph each: lint + same-rows rewrite (`date(ts) = '2025-01-20'` → range filter), budget + human approval, receipts.
8. Limitations: skewed keys, BigQuery/Snowflake beta, heuristic estimates on DuckDB/Postgres.
9. Try it: `pip install gulliver-mcp && gulliver demo`. Repo link. "I'm the author."

**Intro draft (edit to your voice):**

> I gave an AI agent a DuckDB file with 5 million events and asked it to pair up events by user. It wrote a
> self-join on `user_id`. The SQL is correct. It also returns 254,966,150 rows, about 51 times the size of the
> table it started from. On a laptop that is a long wait; on a pay-per-byte warehouse it is a bill.
>
> The agent could not have known. It does not see table sizes, and `EXPLAIN` does not say "this join multiplies
> rows by 51". So I built a tool that makes the agent rehearse first: run the query on 1%, 3% and 10% of the data,
> watch how the output grows, and predict the full run before anyone pays for it.
>
> This post shows how the prediction works, and the mistake it made on my own demo.

### 6.4 X thread (Tue 2026-11-17, 15:30; video uploaded natively)

Replace the transcript with the output of your own recording; do not change the numbers.

**Post 1:**
> An AI agent wrote a self-join on a 5M-row table. Valid SQL. It returns 255 million rows.
>
> I built Gulliver so the agent rehearses on 1%, 3% and 10% of the data first, and sees that coming.
> [native video, 10-30 s]

**Post 2 (transcript-style):**
```
> count events per user pair
gulliver_preflight → status: needs_rehearsal
gulliver_rehearse  → 1%  ~100k rows
                     3%  ~250k rows
                     10% ~2.5M rows
                     exponent > 1.3: grows faster than the data
                     next_step: aggregate before returning rows
```
**Post 3:**
> It also blocks "read-only" SQL that is not read-only: pg_terminate_backend, dblink, pg_read_file,
> DuckDB read_csv('~/.aws/credentials'). 132 bypass cases in the test corpus.

**Post 4:**
> Open source (MIT), MCP server + CLI, DuckDB / Postgres (tested in CI), BigQuery / Snowflake (beta).
> pip install gulliver-mcp && gulliver demo
> https://github.com/laaielichter/Gulliver

**Post 5:**
> Not there yet: Databricks, ClickHouse, and live BigQuery/Snowflake tests.
> If you run agents against a warehouse: what stops a runaway query for you today? I'm the author, replies welcome.

Tag @duckdb only on post 1 or 3 and only once. No tags of accounts that are not involved.

### 6.5 LinkedIn (post one of the two, not both; Dutch is the default for your network)

**Dutch (Wed 2026-11-18, 08:30):**

> AI-agents schrijven steeds vaker SQL op echte data. Ze weten alleen niet hoe groot een tabel is.
>
> Ik liet een agent gebeurtenissen per gebruiker koppelen in een tabel van 5 miljoen rijen. De SQL klopte.
> Het resultaat: 255 miljoen rijen.
>
> Daarom heb ik Gulliver gebouwd, open source. Voordat een agent een query volledig draait, probeert hij hem op
> 1%, 3% en 10% van de data, voorspelt wat 100% gaat doen en draait alleen binnen een budget. Boven de limiet
> moet een mens goedkeuren.
>
> Wat ik leerde: mijn eigen voorspelling zat er op die demo 3 tot 5 keer naast. Als je bij een self-join beide
> kanten samplet, groeit het resultaat kwadratisch. Dat staat nu in de berekening.
>
> Nog niet klaar: BigQuery en Snowflake zijn alleen getest tegen nagebootste verbindingen.
>
> Vraag aan data engineers: laat jij een AI-agent al op productiedata los? Wat houdt een dure query bij jou tegen?
>
> (Link in de eerste reactie.)

**English (alternative, same day and time):**

> AI agents write more and more SQL against real data. They do not know how big a table is.
>
> I let an agent pair up events per user in a 5-million-row table. The SQL was correct. The result: 255 million rows.
>
> So I built Gulliver (open source). Before an agent runs a query in full, it tries it on 1%, 3% and 10% of the
> data, predicts what 100% will do, and only runs inside a budget. Above the limit, a human approves.
>
> What I learned: on that demo my own prediction was 3-5x too low. If you sample both sides of a self-join, the
> result grows with the square of the sample. That is now in the calculation.
>
> Not done yet: BigQuery and Snowflake are only tested against fakes.
>
> Question for data engineers: do you let an AI agent query production data yet? What stops an expensive query?
>
> (Link in the first comment.)

First comment: `https://github.com/laaielichter/Gulliver (MIT). pip install gulliver-mcp && gulliver demo`. Test once whether the link in the body does worse for you; the 2026 data on link-in-comment is mixed.

Note: the "3-5x too low / now in the calculation" lines only go out if fix A0 ships. Otherwise replace them with "it warns, but the number is rough on joins".

### 6.6 Newsletter blurbs (each from your own address, "I'm the author" included)

**Console.dev** (email hello@console.dev, Mon 2026-11-23). Subject: `Gulliver: rehearses AI agents' SQL before full runs`
> Gulliver is an open-source (MIT) MCP server and CLI that makes AI agents test SQL on 1%, 3% and 10% samples,
> estimate cost and run only within a budget, on DuckDB, Postgres, BigQuery and Snowflake. It is pre-1.0 (0.3.0),
> self-service (`pip install gulliver-mcp && gulliver demo`, no sign-up) and documented per feature in the repo.
> https://github.com/laaielichter/Gulliver (demo GIF in the README). I'm the author.

**Data Engineering Weekly** (Tue 2026-11-24, with the r/dataengineering post or the dev.to article as the link; try the newsletter's GitHub PR route first, else LinkedIn message to Ananth Packkildurai). Subject: `Predicting full-run query size from 1/3/10% samples`
> I wrote up how sample-based rehearsal predicts the output size of a query, and why it was off by 3-5x on a
> self-join (both sides sampled scale with p²). The tool is Gulliver, open source, aimed at AI agents writing
> warehouse SQL. [article link] I'm the author.

**Postgres Weekly** (email the Cooperpress editor, Thu 2026-12-03, after the r/PostgreSQL post). Subject: `A read-only gate for agent SQL on Postgres`
> Many database MCP servers treat "only SELECT" as read-only, but `pg_terminate_backend`, `set_config`, `dblink`
> and `pg_read_file` all run from a SELECT. Gulliver 0.3.0 (MIT) refuses these with codes GS001-GS004 and ships
> a 132-case bypass corpus. [link to docs/features/security.md] I'm the author.

**Python Weekly** (reply to an issue or use any contact found, Thu 2026-11-26). Subject: `Gulliver: a Python MCP server that rehearses SQL`
> Gulliver is a Python (3.10-3.13) MCP server and CLI, built on sqlglot, that lints, rewrites and rehearses
> agent-written SQL on samples before a full run. `pip install gulliver-mcp && gulliver demo`.
> https://github.com/laaielichter/Gulliver. I'm the author.

**Data Elixir** (form at dataelixir.com/contact, Mon 2026-11-30; lower fit). Use the Data Engineering Weekly text.

**PulseMCP / Glama newsletters:** no pitch; they come from the listings (section 5, rows 2 and 10).

---

## 7. Six-week calendar (all times Europe/Amsterdam)

**Before the calendar**
- Week of **Nov 2-8**: fixes A0-A10 on PR #17, social preview, Discussions, topics, Glama claim.
- **Mon Nov 9**: release 0.3.0 (GitHub, PyPI, npm, MCP Registry, plugin).
- **Nov 10-15**:
  - Tue Nov 10: Claude plugin directory submission.
  - Thu Nov 12: mcp.so and mcpservers.org.
  - All week: record the demos, outsider test, take part in the target subs without links, write the Show HN text.

**Go / no-go:** Sun 2026-11-15, 20:00. Check every row of the readiness gate (section 2). If one is missing, move the whole calendar one week later.

| Week | Date and time | Action |
|---|---|---|
| **1** (Nov 16-22) | Mon 16, 10:00 | awesome-claude-code web form (entry B) |
| | **Tue 17, 14:00** | **Show HN** (your own text). Online until 22:00, reply to every comment |
| | Tue 17, 15:30 | X thread with the native video |
| | Wed 18, 08:30 | LinkedIn (Dutch) |
| | Wed 18, 09:30 | dev.to article |
| | Wed 18, 15:30 | r/mcp |
| | Thu 19, 15:30 | r/ClaudeCode |
| | Fri 20 | r/ClaudeAI showcase megathread comment. Check Glama score; if present, awesome-mcp-servers PR (entry A) and the README badge (A10) |
| | Sun 22 | Weekly review (section 8) |
| **2** (Nov 23-29) | Mon 23, 10:00 | Console.dev email; awesome-dbt PR or form (entry C) |
| | Tue 24, 10:00 | awesome-duckdb PR (entry D) |
| | Tue 24, 15:30 | r/dataengineering technical post |
| | Tue 24 | Data Engineering Weekly pitch |
| | Thu 26, 10:00 | Python Weekly |
| | Thu 26, 15:30 | r/duckdb |
| | Fri 27 | Short "what I learned from launching" post: GitHub Discussions + LinkedIn comment thread |
| | Sun 29 | Weekly review; label 1-2 new good first issues from feedback |
| **3** (Nov 30-Dec 6) | Mon 30 | Data Elixir form; check PulseMCP submissions |
| | Tue Dec 1, 10:00 | awesome-data-engineering PR (entry F), **only** if there is outside use (an outside issue, PR or comment) |
| | Wed Dec 2, 15:30 | r/PostgreSQL (security gate) |
| | Thu Dec 3, 09:30 | dev.to article 2: "132 ways an agent can escape a read-only SQL gate" (your text; real corpus cases) |
| | Thu Dec 3 | Postgres Weekly email |
| | Sun 6 | Weekly review |
| **4** (Dec 7-13) | Mon 7 | Release 0.3.1 with a changelog: fixes from launch feedback |
| | Tue 8, 15:30 | r/bigquery, only with a real BigQuery example. Otherwise ask in Discussions for a BigQuery tester and skip |
| | Thu 10, 15:30 | r/snowflake, same condition |
| | Thu 10, 15:30 | X: one short post on the 0.3.1 change, with a GIF |
| | Sun 13 | Weekly review |
| **5** (Dec 14-20) | Tue 15, 09:30 | Use-case post: `gulliver review` on a real dbt project PR (screenshot of the sticky comment), on dev.to and LinkedIn |
| | Wed 16 | Smithery (optional). Re-share on the one platform with the most referral traffic so far (one post, new angle, not a repost) |
| | Thu 17 | Answer every open issue and Discussion; close or label stale ones |
| | Sun 20 | Weekly review |
| **6** (Dec 21-27) | Holiday week: no posts | |
| | Mon 21 | Answer issues |
| | Sun 27 | 6-week retrospective: stars, downloads, referrers, contributors vs the baseline. Pick the January plan: awesome-postgres only with real adoption; Product Hunt only with a visual demo; relaunch only for a significant release (e.g. live-tested BigQuery/Snowflake) |

Every day in weeks 1-2: reply to all issues, PRs, Discussions and post comments within 24 hours.

---

## 8. Metrics baseline and weekly review

### Baseline (2026-11-02, before any launch activity)

| Metric | Value | Source |
|---|---|---|
| Stars | 0 | api.github.com/repos/laaielichter/Gulliver |
| Forks | 2 | same |
| Watchers (subscribers) | 0 | same |
| Open issues (API count) | 7: 5 issues (#4, #5, #7, #11, #12) + 2 open PRs (#17 draft, #8) | same, and /pulls |
| Repo created / last push | 2026-11-01 17:13 UTC / 2026-11-02 19:57 UTC | same |
| PyPI downloads | last_day 94, last_week 0, last_month 0 (pypistats has not caught up for a 1-day-old package; early counts include mirrors and bots) | pypistats.org/api/packages/gulliver-mcp/recent |
| npm downloads | 288 for 2026-10-26 to 2026-11-01 (also includes mirrors; the package is a day old) | api.npmjs.org/downloads/point/last-week/gulliver-mcp |
| MCP Registry | not listed | registry search |
| Glama | listed, score pending, v0.1.0 | glama.ai |
| GitHub traffic | **not captured**: the `gh` CLI cannot read traffic in this session. Run the snapshot script yourself today (below) | |

### Set up the traffic snapshot (do this today; GitHub only keeps 14 days)

Run it on your own machine, logged in with `gh auth login` as `laaielichter` (needs push access):

```bash
python3 /path/to/repo-growth/scripts/traffic_snapshot.py laaielichter/Gulliver growth
```

It appends to `growth/traffic.csv` (daily views and clones), `growth/referrers.csv` and `growth/paths.csv`
(14-day totals per snapshot; do not add snapshots together) and `growth/stars.csv` (stars, forks, watchers,
open issues). Keep `growth/` outside the repo, or in a private repo, if you do not want traffic in public.

To run it every Sunday at 19:40 (crontab, Amsterdam time):
```
40 19 * * 0 cd ~/gulliver-growth && python3 /path/to/traffic_snapshot.py laaielichter/Gulliver growth
```
Run it daily from Tue Nov 17 to Sun Nov 22, because launch referrers drop out after 14 days.

Downloads, weekly:
```bash
curl -s https://pypistats.org/api/packages/gulliver-mcp/recent
curl -s https://api.npmjs.org/downloads/point/last-week/gulliver-mcp
```

### Weekly 20-minute review (every Sunday, 20:00)
1. Run the traffic snapshot.
2. Write down stars, forks, watchers, open issues, PyPI and npm weekly downloads, and Glama/Registry status in one row of a sheet.
3. Look at the top referrers. Note any new source and take part there (answer, do not post links).
4. Reply to every new issue, PR and Discussion. Label 1-2 new good first issues so there are always 3-5 open.
5. Pick one content action for next week from the calendar, or move it if the data says so.
6. Write one line: what moved the needle this week.

Signals that matter more than stars: PyPI/npm weekly downloads after week 2 (when mirror noise settles), outside issues and PRs, Discussions posts, returning visitors, and plugin installs.
