# Demo video — spoken script

**Runtime target: ~3 minutes** (no video-length requirement is stated anywhere
in the repo, so this follows the "unspecified → 3 min" default). It runs about
**3:05** read at a normal pace; if you need 3:00 flat, cut the ladder sentence
in Beat 3 or the third limitation in Beat 6.

The script follows [`EVALUATION.md`](../EVALUATION.md): premise → the two data
sources → one decision explained → why we hold customers back → the verdict →
the limitations. It **ends on the limitations**, not a pitch.

---

## BEFORE YOU RECORD — setup so the demo cannot fail live

### 1. Build the demo database (once, ~1 minute, deterministic)

```
python scripts/make_demo_db.py
```

Writes `data/demo.db` — the 2,000-event corpus through the real pipeline,
**1,611 decisions**. Same seed, byte-identical every run.

### 2. Confirm the real live DB has exactly one decision

Beat 2 depends on this being **1**.

```
python -c "import sqlite3; print(sqlite3.connect('webhook_events.db').execute('select count(*) from decisions').fetchone())"
```

Expect `(1,)`. If it is not 1, the "one real decision" line in Beat 2 is wrong —
stop and check which `webhook_events.db` you are pointed at.

### 3. Start TWO servers and leave both running

**Server A — the real live webhook DB.** New terminal. Do **not** set
`DASHBOARD_DB_PATH` here.

```
# PowerShell
uvicorn app.main:app --port 8000
```

**Server B — the simulated corpus DB.** Another new terminal.

```
# PowerShell
$env:DASHBOARD_DB_PATH = "data/demo.db"; uvicorn app.main:app --port 8001
# bash
DASHBOARD_DB_PATH=data/demo.db uvicorn app.main:app --port 8001
```

Server A serves the live DB (1 decision, **no banner**). Server B serves the
corpus (1,611 decisions, **"Corpus run — outcomes are simulated" banner on every
page**). You never switch a server mid-recording — you switch browser tabs.

### 4. Open these tabs, in this order, and HARD-REFRESH each once so it is warm

| Tab | URL | Used in | Why pre-warm |
|---|---|---|---|
| 1 | `http://localhost:8000/metrics` | Beat 2 | first hit parses `results/final_run.json` and derives the LIVE panel |
| 2 | `http://localhost:8001/decisions` | Beat 3 | **slowest page — ~1,600 table rows.** Must be fully rendered before you click. |
| 3 | `http://localhost:8001/trace/pay_000001` | Beat 3 | first hit compiles the template, reads the policy, recomputes the ladder |
| 4 | `http://localhost:8001/not-chased` | Beat 4 | 485 rows + the empty-group lines |
| 5 | `http://localhost:8001/queue` | Beat 6 | trivial, but pre-load anyway |
| 6 | `EVALUATION.md` (rendered, scrolled to the bold quote at the top) | Beat 5 | no server; just have it ready |

### 5. Browser

Zoom to **115–125%** so the numbers are legible on video. Pick light or dark
(the pages follow the OS theme) and lock it. Close every other tab.

---

## SCRIPT

Legend: **[ON SCREEN]** = what the viewer sees and where you point.
**⚠ PRE-WARM** = do not speak this beat over a page that has not already loaded.

---

### Beat 1 — The premise · 0:00–0:35 · NO BROWSER

**[ON SCREEN]** You to camera, or a plain title card. No browser yet.

> Online payments fail all the time. A card gets declined. A bank times out.
>
> Here is the catch. A lot of those customers just try again a bit later and
> pay on their own. Nobody had to do anything.
>
> So if you send everyone a reminder, and then count how many paid, that number
> barely means anything. Most of them would have paid anyway.
>
> The number that matters is the extra one. How many payments came back
> *because* you reached out — on top of the ones that would have come back
> untouched. That is the incremental recovery.
>
> And to measure it, you need a group of customers you deliberately never
> contact. A control group. This whole project is built around that one idea.

---

### Beat 2 — Two sources: live vs simulated · 0:35–1:05

**[ON SCREEN]** Tab 1 — `http://localhost:8000/metrics`.
Point at the two stacked panels: **LIVE** (teal) on top, **CORPUS RUN** (amber)
below. Point at the LIVE panel's "Events processed" figure — it reads **1**.
Then point at the amber badge on the lower panel:
*"simulated outcomes · eval harness · not live traffic"*.
⚠ PRE-WARM Tab 1.

> This is the dashboard. Two sources, side by side. They are never added
> together.
>
> The top panel is live traffic — real webhooks that actually reached this
> server. Right now that is exactly one decision. One real payment.
>
> The bottom panel is a simulated run. Two thousand made-up failures, pushed
> through the exact same code. Every amber number here is a simulation. It is
> not production — and the dashboard says so, right here.

---

### Beat 3 — One decision, explained · 1:05–1:55

**[ON SCREEN]** Switch to Tab 2 — `http://localhost:8001/decisions`. The
"Corpus run — outcomes are simulated" banner is now pinned to the top of every
page. Point at the group headers: **ACT — 1126**, **SKIP — 485**.
Click the first row under ACT, `pay_000001` → lands on Tab 3,
`http://localhost:8001/trace/pay_000001`.
Point at **Section 1, "The verdict"** and read it.
Point at **Section 3, "The arithmetic"** — the three lines of the sum.
Point at **Section 4, "The ladder"** — the five rows.
⚠ PRE-WARM Tab 2 and Tab 3. Do not click through from a cold `/decisions`.

> Every decision is on the record. Here is one we acted on. *[click]*
>
> Top of the page, in plain English: we emailed this customer, because the
> expected value of doing it beat our cut-off. Expected value just means — how
> likely is it to work, times how much it is worth, minus what it costs.
>
> Here is that sum with the real numbers. About a five percent chance the nudge
> works. Five percent of a sixteen-hundred-rupee payment is roughly eighty-seven
> rupees. The email costs ten paise. Eighty-seven is way over our two-rupee
> floor, so we send it. Nothing flagged it — no safety check stepped in.
>
> And this is the ladder — five ways to reach someone, cheapest first. Email
> won. The three phone options are greyed out, because we have no phone number
> for this customer.

---

### Beat 4 — Why we hold customers back · 1:55–2:20

**[ON SCREEN]** Tab 4 — `http://localhost:8001/not-chased`.
Point at the lead paragraph. Point at the summary line:
*"485 decision(s) not chased — Rs 416,630.00 of ticket value withheld from
contact."* Point at the first group: **"Held back by experimental design — 485"**.
⚠ PRE-WARM Tab 4.

> This is the part that makes the result mean something. Four hundred and
> eighty-five customers we chose not to contact. Between them, they were trying
> to pay more than four hundred thousand rupees.
>
> We held them back on purpose. They are the control group. Their recovery rate,
> with no help from us, is the baseline. Without them, we would have nothing to
> compare against — and the whole result would just be a guess.

---

### Beat 5 — The verdict · 2:20–2:48

**[ON SCREEN]** Tab 6 — `EVALUATION.md`, rendered, scrolled to the bold
block-quote at the top. Point at that sentence.

> So what does the simulated run show?
>
> The intervention works. Reaching out caused clearly more recoveries, and that
> holds up statistically — the confidence interval does not touch zero.
>
> Does it pay for itself? We cannot say yet. On average the money number is
> positive, but the error bars cross zero. We might be making money. We cannot
> rule out losing a little.
>
> Works — yes. Proven to pay — not on this data. Those are two different claims,
> and we only stand behind the first one.

---

### Beat 6 — Limitations · 2:48–3:05 · END HERE

**[ON SCREEN]** Tab 5 — `http://localhost:8001/queue`. The banner is still
pinned at the top. The page shows the empty "human queue" state. Point at the
banner. Then point at the paragraph headed *"Every LLM-classified failure."*

> Three things to be straight about.
>
> One — every outcome you just saw is simulated. The only real recovery signal
> is a live payment confirmation, and there are none in this data.
>
> Two — the test data has no phone numbers. So the ladder never got past email.
> SMS, WhatsApp, and calls are built, but untested against real data.
>
> Three — there is an AI classifier for the ambiguous failures. It ships
> switched off from spending any money. It stays off until it is checked against
> real outcomes. This screen is where those cases would wait — and it is empty.
>
> That is the honest state of it.

*[end — no outro, no logo, no call to action]*

---

## Claim → what backs it on screen

| Spoken claim | Shown |
|---|---|
| "exactly one real decision" | `/metrics` (Server A) LIVE panel, "Events processed" = 1 |
| "two thousand simulated failures … not production" | `/metrics` CORPUS panel + its "simulated outcomes · not live traffic" badge |
| "we emailed this customer because the expected value beat our cut-off" | `/trace/pay_000001` Section 1 verdict sentence |
| "~5% × ₹1,630 − ₹0.10 = ~₹86.68, over the ₹2 floor" | `/trace/pay_000001` Section 3, the arithmetic block |
| "phone options greyed out — no phone number" | `/trace/pay_000001` Section 4, ladder rows for sms / whatsapp / agent_call |
| "485 customers, ₹416,630 withheld, held back on purpose" | `/not-chased` summary line + "Held back by experimental design" group + lead paragraph |
| "works, not proven to pay; the interval crosses zero" | `EVALUATION.md` bold verdict block-quote |
| "outcomes simulated; no live payment confirmation in this data" | `EVALUATION.md` limitation 1 |
| "no phone numbers; ladder collapses to email" | `EVALUATION.md` limitation 2 + the greyed rungs from Beat 3 |
| "AI classifier shipped barred from spending; queue is empty" | `/queue` empty-state text ("TAIL_ACT_ENABLED is False", "the LLM path was never used") |
