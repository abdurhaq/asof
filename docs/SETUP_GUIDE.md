# Setup guide (Windows, step by step)

Time needed: about 30 minutes of your attention plus about 1–2 hours of downloading in the background.

## 0. Prerequisites

- **Python 3.10, 3.11 or 3.12** — check with `python --version` in PowerShell. If missing, install from
  python.org and tick *"Add python.exe to PATH"*.
- **Git** (optional, to push to GitHub).
- About 3 GB of free disk (PyTorch + models + data).

## 1. Open the project in PowerShell

```powershell
cd "C:\Users\Abdur Rahman\OneDrive\Documents\AMITY UNIVERSITY\Project - AsOf\asof"
```

> Tip: OneDrive can slow down folders with thousands of small files (the virtual environment, the downloaded pages).
> If installs feel slow, copy the `asof` folder to `C:\dev\asof` and work there.

## 2. Create a virtual environment and install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .                       # makes "python -m asof.cli" work from anywhere in the project
python -m pytest -q                    # should print "25 passed"
```

## 3. Get your free API keys

### Groq (the LLM)
1. Go to https://console.groq.com and sign in with Google.
2. **API Keys → Create API Key**, copy it.

### Neo4j AuraDB Free (the graph database)
1. Go to https://console.neo4j.io and sign up.
2. **New instance → AuraDB Free**. Download the credentials file when it is shown (the password is shown only once).
3. You need the **Connection URI** (looks like `neo4j+s://abcd1234.databases.neo4j.io`), username `neo4j` and the password.
   Free instances pause after a few days without use; resume them from the console.

### Put them in `.env`
```powershell
copy .env.example .env
notepad .env
```
Fill in `GROQ_API_KEY`, `NEO4J_URI`, `NEO4J_PASSWORD`. Save.

> Everything also works without keys: no `GROQ_API_KEY` → an offline "extractive" answerer is used;
> no `NEO4J_URI` → an in-memory graph is used. Good for development, but use both for the real project.

## 4. Download the RBI documents

The downloader waits 1.5 s between requests to be polite to the RBI website, and caches every page in `data\raw\`
— if it stops, just run the same command again and it continues.

```powershell
python -m asof.cli fetch --source md     # all current Master Directions        (~5–10 min)
python -m asof.cli fetch --source mc     # current Master Circulars             (~3–5 min)
python -m asof.cli fetch --source ids --start 11300 --end 13720   # notifications mid-2018 → 2026 (~1 hour)
```

The third command is what gives the project its **history** (older circulars, amendments, and older editions
of master circulars). For an even richer corpus, run it again for an older range, e.g. `--start 9500 --end 11299`.

## 5. Build the index

```powershell
python -m asof.cli build
```

This parses the pages, extracts relations, computes validity intervals, chunks, embeds (first run downloads
`bge-small`, ~130 MB) and loads the graph into Neo4j. Read the printed summary: number of documents,
relations by type, how many documents were superseded, and three sample titles.
If the samples look wrong (e.g. "Untitled"), open one HTML file from `data\raw\` and check which table holds the text (see `ingest/parse.py`).

Look at the graph in Neo4j: open your instance in the Aura console → **Query**, and run
```cypher
MATCH p=(:Document)-[:SUPERSEDES|AMENDS*1..3]->(:Document) RETURN p LIMIT 50
```
That picture is great for your README and interviews.

## 6. Ask a question from the terminal

```powershell
python -m asof.cli ask "What were the rules on internal ombudsman for banks as of June 2022?"
```

## 7. Run the benchmark

```powershell
python -m asof.cli eval                          # retrieval metrics for 3 systems, no API calls
python -m asof.cli eval --answers --limit 40     # + answer metrics with Groq (uses ~120 API calls)
```

Results are written to `data\eval\results.md`. Copy the table into the README.

**Also measure relation precision** (strong interview material):
```powershell
python -m asof.cli audit
```
Open `data\eval\relations_audit.csv` in Excel, label 50 random rows `y`/`n` in the last column
by reading the evidence sentence, and report *"relation extraction precision: 46/50 = 92%"*.

Optional: write 20–30 of your own questions in `data\eval\manual_questions.jsonl`
(format in `manual_questions.example.jsonl`; get doc ids from the app's document drawer or `data\processed\documents.jsonl`).

## 8. Start the web app

```powershell
python -m asof.cli serve
```
Open http://127.0.0.1:8000. Try the example questions, drag the time slider, and switch on
**Compare with naive RAG** to see stale citations highlighted in red. Take a screenshot and save it as
`docs\img\screenshot.png`.

## 9. Deploy on AWS Lambda (about USD 0-0.50 a month)

Hugging Face Spaces now need a paid PRO plan for Docker apps, so AsOf runs **serverless on AWS Lambda**:
there is no server running all month. The app sleeps when nobody uses it and wakes up by itself when
someone opens the link, so the link on your resume always works. Lambda's monthly free allowance
(1 million requests, 400,000 GB-seconds) covers a portfolio project; what you pay from your credits is
image storage (a few cents a month) plus a few cents per deploy for the temporary build server.

**One-time: create an access key (5 minutes)**
1. Sign in at https://console.aws.amazon.com, switch the region (top right) to **Asia Pacific (Mumbai)**,
   and open **IAM -> Users -> Create user**. Name it `asof-deployer` (no console access needed). Next.
2. **Attach policies directly** and tick these six:
   `AWSLambda_FullAccess`, `AmazonEC2ContainerRegistryFullAccess`, `AmazonEC2FullAccess`,
   `AmazonS3FullAccess`, `IAMFullAccess`, `AmazonEventBridgeFullAccess`. Next -> Create user.
3. Open the user -> **Security credentials -> Create access key** -> choose *Command Line Interface (CLI)*,
   tick the confirmation, Next -> **Create access key**. Keep this page open (the secret is shown only once).
   Never paste these keys into a chat, email or the project files.

**Deploy**
1. Double-click **`deploy_aws.bat`**. Paste the *Access key ID*, then the *Secret access key* (the second
   one stays hidden while you paste). Answer `Y` to save them in your Windows user's AWS credentials file,
   so you are not asked again.
2. The script uploads the app to a private S3 bucket and starts a temporary build server. That server builds
   the app image, stores it in your private registry and deletes itself (about 10-15 minutes; a line
   is printed every minute).
3. It then creates the Lambda function and its public HTTPS link, and prints
   **`AsOf is live: https://....lambda-url.ap-south-1.on.aws/`**. That link never changes; put it on your resume.

Your Groq and Neo4j values go from `.env` into the function's encrypted settings. They are never part of
the image or the uploaded files. The public app allows 30 questions per hour per visitor, to protect your
Groq free quota (change `ASK_LIMIT_PER_HOUR` in `.env` and redeploy).

**Keep-warm.** By default the app is pinged every 5 minutes, so it stays loaded and visitors rarely wait.
This stays inside the free allowance. With keep-warm off, the first visit after a quiet spell takes about
30-60 seconds and later questions are fast. Switch it in `aws_app.bat` (options 3 and 4).

**Later**
- After `rebuild.bat` or a code change, run `deploy_aws.bat` again. The link stays the same, and the old
  version keeps serving until the new one is ready.
- `aws_app.bat` -> status and link, recent app log (the first place to look if something fails),
  keep-warm on/off, or delete everything AsOf created on AWS.
- AuraDB Free pauses after a few days without use; if answers fail with a Neo4j error, resume it in the
  Aura console.

Also push the code (without `data` and `.env`) to GitHub for your resume link.

(`deploy_hf.bat` still works if you ever take Hugging Face PRO; the same Dockerfile runs on any Docker host.)

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: asof` | Run `pip install -e .` inside the activated venv |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Fetch prints many `!` errors | RBI site is slow or rate-limiting — increase `--delay 3` and re-run |
| Groq `429 rate limit` | Free tier limits; the code retries. Use `--limit` in eval, or wait a minute |
| Neo4j `ServiceUnavailable` | The AuraDB Free instance paused — resume it in the console |
| `deploy_aws.bat`: `AccessDenied` / `UnauthorizedOperation` / `not authorized` | The IAM user is missing one of the six policies in section 9 |
| `deploy_aws.bat`: "The image build failed" | Read `logs\aws_build.log` (the failing Docker step is near the end); the live app was not changed |
| The link shows an error after a deploy | `aws_app.bat` -> 2 shows the recent app log with the error |
| Torch install is huge/slow | `pip install torch --index-url https://download.pytorch.org/whl/cpu` first, then the requirements |
