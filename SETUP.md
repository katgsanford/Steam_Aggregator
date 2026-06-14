# Steam Review Aggregator — Setup Guide

This guide walks you through everything from a blank Windows machine to a fully
running dashboard. No prior technical knowledge is assumed.

---

## Part 1 — Install the Prerequisites

### 1.1 Install Python 3.12 (this exact version matters)

This project requires **Python 3.12** — not the newest release, and not an older
one. The update-tracking feature talks to Steam through a library (`steam`) whose
dependencies (`gevent`, `protobuf`) only work correctly on 3.12. Python 3.13 and
3.14 removed internal pieces that library still needs, so it fails to load there.
Install **3.12 specifically**.

1. Open your web browser and go to **https://www.python.org/downloads/windows/**
2. Find the most recent **"Python 3.12.x"** release (e.g. 3.12.7) and click its
   **"Windows installer (64-bit)"** link. Do **not** use the big button at the top
   of the main downloads page — that's the newest version, which will not work here.
3. Run the downloaded installer (`.exe` file).
4. **Important:** On the first screen of the installer, check the box that says
   **"Add python.exe to PATH"** before clicking Install Now.
5. Click **"Install Now"** and let it finish, then click **"Close"**.

**Verify it worked:**
- Press `Windows key + R`, type `cmd`, press Enter.
- In the black window that opens, type: `py -3.12 --version` and press Enter.
- You should see `Python 3.12.x`. If you do, Python 3.12 is installed correctly.

> **Already have a different Python version?** (3.11, 3.13, 3.14, …) — no problem,
> and do **not** uninstall it. Just install 3.12 *alongside* it with the steps
> above. Windows keeps multiple versions side by side, and the **`py` launcher**
> lets you choose: `py -3.12` always runs 3.12 specifically, regardless of what
> your plain `python` command points to. Run `py --list` to see every version you
> have. This guide uses `py -3.12` for the one-time setup so you always get the
> right interpreter; after Part 3.2, the project's virtual environment locks
> everything to 3.12 automatically and you won't have to think about it again.

---

### 1.2 Install Git

Git is the tool that lets you download the project code from GitHub.

1. Go to **https://git-scm.com/download/win**
2. The download should start automatically. Run the installer.
3. Click **"Next"** through all the screens — the defaults are fine.
4. Click **"Finish"** when done.

**Verify it worked:**
- Open a new Command Prompt (`Windows key + R` → `cmd` → Enter).
- Type: `git --version` and press Enter.
- You should see something like `git version 2.45.0`.

---

### 1.3 Get an Anthropic API Key

The dashboard uses Anthropic's AI to generate review themes and summaries.
You need an account and API key to enable that part.

1. Go to **https://console.anthropic.com** and create an account (or sign in).
2. Once logged in, click **"API Keys"** in the left sidebar.
3. Click **"Create Key"**, give it a name (e.g. `steam-reviews`), and click **"Create"**.
4. **Copy the key immediately** — it starts with `sk-ant-` and you won't be able to
   see it again after closing the dialog. Paste it somewhere safe (like Notepad) for now.

> The AI features cost a small amount per use (fractions of a cent per analysis).
> You will need to add a payment method in the Anthropic console. Without a key,
> the charts and data collection still work — only the AI summaries are unavailable.

---

## Part 2 — Get the Code

### 2.1 Accept the GitHub Invitation

If you were shared access to a private repository, you will receive an email from
GitHub with a subject like **"You've been invited to collaborate"**.

1. Open that email and click the **"Accept invitation"** button.
2. If you don't have a GitHub account yet, you'll be prompted to create one (free).
   Create an account, then return to the invitation email link and accept.

### 2.2 Download the Code

1. Press `Windows key + R`, type `cmd`, press Enter to open a Command Prompt.
2. Decide where you want to store the project. A simple location is your Documents
   folder. Type the following and press Enter:
   ```
   cd %USERPROFILE%\Documents
   ```
3. Now download ("clone") the code by typing:
   ```
   git clone https://github.com/katgsanford/Steam_Aggregator.git
   ```
   You may be prompted to sign in to GitHub — follow the prompts in your browser.
4. When it finishes, a folder called `Steam_Aggregator` will appear in your Documents.

---

## Part 3 — Configure the Project

### 3.1 Open the Project Folder

In the same Command Prompt window, type:
```
cd Steam_Aggregator
```
You are now inside the project folder. All future commands in this guide assume
you are in this folder.

### 3.2 Create the Virtual Environment (Python 3.12)

A "virtual environment" (venv) is a private, self-contained copy of Python 3.12
that lives inside the project folder. Everything we install goes into it, so the
project always runs on 3.12 no matter what other Python versions are on your
machine — and nothing here can disturb them.

1. Create it (this is where `py -3.12` guarantees the right version):
   ```
   py -3.12 -m venv .venv
   ```
2. **Activate it:**
   ```
   .venv\Scripts\activate
   ```
   Your command prompt line will now start with `(.venv)`. That tells you the
   environment is active.

> **Important — you must activate the venv in every new Command Prompt window**
> before running any `python ...` command from this project (Parts 4–7). If you
> close the window and come back later, `cd` into the project folder and run
> `.venv\Scripts\activate` again first. You'll know it worked when you see
> `(.venv)` at the start of the line. To double-check you're on 3.12, run
> `python --version` while activated — it should say `Python 3.12.x`.

### 3.3 Install the Required Python Packages

With the venv **active** (you see `(.venv)`), type the following and press Enter.
This downloads all the libraries the project needs into the venv:
```
python -m pip install -r requirements.txt
```
Wait for it to finish. You will see a lot of text scroll by — that is normal.
This installs the Steam update-tracking library (`steam`) too, which is why the
3.12 venv matters.

### 3.4 Create Your Configuration File

The project needs to know your Anthropic API key. We store it in a file called `.env`
so it never ends up in the shared code.

1. In the Command Prompt, type:
   ```
   copy .env.example .env
   ```
2. Open the `.env` file in Notepad:
   ```
   notepad .env
   ```
3. You will see lines like:
   ```
   ANTHROPIC_API_KEY=your-key-here
   ```
   Replace `your-key-here` with the key you copied in Step 1.3. It should look like:
   ```
   ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
   ```
4. Save the file (`Ctrl+S`) and close Notepad.

---

## Part 4 — Add Your First Game

### 4.1 Run the Setup Tool

The setup tool lets you search for a Steam game and pick which languages to collect
reviews in.

In the Command Prompt (make sure you are still in the `Steam_Aggregator` folder
and the venv is active — you should see `(.venv)` at the start of the line; if not,
run `.venv\Scripts\activate`), type:
```
python setup.py
```

The tool will:
1. Ask you to type a game name — type it and press Enter.
2. Show you a numbered list of matching Steam titles — type the number of the right
   one and press Enter.
3. Show you the languages that game supports — type the numbers of the languages
   you want (separated by commas), or type `all` for every language, then press Enter.
4. Confirm the game has been added.

Repeat this step for each additional game you want to track.

---

## Part 5 — Collect Reviews for the First Time

This step downloads all existing reviews for your added games. For a popular title
this can take **10–30 minutes** on the first run — subsequent daily runs will only
fetch what's new and take seconds to a few minutes.

In the Command Prompt (venv active — `(.venv)` showing), type:
```
python collect.py
```

You will see progress messages as each language is collected. When it finishes you
will see a summary like:
```
Run done: 45321 new reviews total -> reviews.db
```

Alongside reviews, each run also refreshes the list of game updates ("releases")
two ways: from Steam's posted patch notes, and from Steam's build system (PICS),
which catches updates the developer shipped without posting notes. The PICS part
is why this must run inside the 3.12 venv.

---

## Part 6 — Schedule the Daily Automatic Collection

This sets up Windows to automatically run the collector every day at noon so your
data stays current without you having to do anything.

In the Command Prompt, type:
```
powershell -ExecutionPolicy Bypass -File .\register-task.ps1
```

If Windows asks for administrator confirmation, click **Yes**. (You do not need the
venv active for this one step — the script automatically finds and uses the venv's
Python so the daily run always has the update-tracking library available.)

**To verify the schedule was created:**
```
schtasks /Query /TN "SteamReviewCollector"
```
You should see a row showing the task is `Ready` and scheduled for `12:00 PM`.

**Useful schedule commands:**
| What you want to do | Command |
|---|---|
| Run the collector right now | `schtasks /Run /TN "SteamReviewCollector"` |
| Check the schedule status | `schtasks /Query /TN "SteamReviewCollector"` |
| Remove the schedule | `schtasks /Delete /TN "SteamReviewCollector" /F` |

---

## Part 7 — Open the Dashboard

### 7.1 Start the Dashboard Server

In the Command Prompt (in the `Steam_Aggregator` folder, venv active — if this is a
fresh window, run `.venv\Scripts\activate` first so you see `(.venv)`), type:
```
python app.py
```

You will see:
```
 * Running on http://127.0.0.1:5000
```
**Leave this Command Prompt window open** — closing it stops the dashboard.

### 7.2 Open the Dashboard in Your Browser

Open your web browser and go to:
```
http://127.0.0.1:5000
```

The dashboard will load with your game(s) in the Title dropdown.

### 7.3 Stop the Dashboard

When you are done, click back on the Command Prompt window and press `Ctrl+C`.

---

## Part 8 — Using the Dashboard

### Selectors (top of the page)

| Selector | What it does |
|---|---|
| **Title** | Switch between games you are tracking |
| **Scope** | *All time* shows the full history. Individual releases (★ = major) window the data to just that patch period. |
| **Language** | Filter by review language. Languages marked ⭐ get their own AI analysis. Others still filter the chart but share the all-languages AI output. |

In the **top-right corner** there is a **🌙 Dark / ☀ Light** button that switches the
whole dashboard between dark and light mode. It defaults to dark and remembers your
choice in that browser.

### Panels (in order)

**KPI cards (top row)** — At-a-glance headline numbers: total reviews and % positive
for the current Scope/Language (with how far that differs from the game's overall
average), plus how many languages are collected, the latest release, and when the data
was last updated.

**Sentiment over time** — A chart showing the % of positive reviews over time. It draws
two lines: a smooth **cumulative** line (the running overall average) and a **per-period**
line (each day/week/month on its own). Buckets are daily for short windows, weekly, or
monthly for long histories. On the all-time view, major releases appear as dashed
vertical lines.

**Language breakdown** — A table showing each language's review count, % positive,
and how much it differs from the overall average. The badge in the last column tells
you whether that language gets its own AI analysis or shares the all-languages one.

**Common review themes** — The top 5 things players like and dislike, extracted by
AI from the most-upvoted reviews. On the all-time view (and the current release) there
is also a "Last 7 days" block showing the most recent themes. These are cached and only
regenerate when enough new reviews have arrived (or once per day for the 7-day block).
If a view has very few reviews you will see a small-sample warning or a "not enough
reviews" note instead of a full analysis.

**Executive summary** — Two AI-written reports in one panel with tabs at the top:
**Product stakeholders** (sentiment trajectory, opportunities, churn risk) and
**QA team** (reported defects, stability issues, suggested test priorities). Click a tab
to switch between them.

**Regenerate button** — Forces the AI to rebuild the analysis for the current view
right now, regardless of how many new reviews have arrived. Use this sparingly as
it costs API tokens.

---

## Part 9 — Adding More Games Later

To track an additional game at any time:

1. Open a Command Prompt, navigate to the project folder, and activate the venv:
   ```
   cd %USERPROFILE%\Documents\Steam_Aggregator
   .venv\Scripts\activate
   ```
   You should now see `(.venv)` at the start of the line.
2. Run the setup tool:
   ```
   python setup.py
   ```
3. Follow the prompts to search for and add the new game.
4. Run the collector once to backfill its history:
   ```
   python collect.py
   ```
   The new game will also be picked up automatically by the daily scheduled run
   going forward.

---

## Troubleshooting

**"python is not recognized"**
Either Python wasn't added to PATH during install, or you forgot to activate the
venv. First make sure the venv is active (`cd` into the project folder and run
`.venv\Scripts\activate` — you should see `(.venv)`). If `py -3.12 --version` also
fails, re-run the Python 3.12 installer, choose "Modify", and check the
"Add python.exe to environment variables" box.

**Updates aren't being detected / "No module named 'steam'"**
The update-tracking step needs the project's 3.12 venv. Make sure you created it
(`py -3.12 -m venv .venv`), activated it (`.venv\Scripts\activate`), and ran
`python -m pip install -r requirements.txt` while it was active. Reviews and posted
patch notes still collect without it, but silent updates won't be picked up. If
`python --version` shows anything other than `3.12.x`, the venv isn't active.

**"No titles tracked yet"**
You need to run `python setup.py` to add at least one game before collecting or
opening the dashboard.

**Dashboard shows no AI summaries / "ANTHROPIC_API_KEY is not set"**
Open the `.env` file in Notepad and make sure your API key is on the
`ANTHROPIC_API_KEY=` line with no extra spaces. Restart `python app.py` after saving.

**"database is locked"**
The collector and dashboard are both writing at the same time. This is rare and
self-resolves. If it persists, stop the dashboard (`Ctrl+C`), let the collector
finish, then restart the dashboard.

**The dashboard page is blank / nothing loads**
Make sure you opened `http://127.0.0.1:5000` in your browser — not by double-clicking
the HTML file. The `python app.py` command must be running in a Command Prompt.
