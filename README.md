# Order Win Tracker (BSE + NSE)

Checks BSE and NSE every evening for new order-win announcements, adds them to `data/orders.csv`, and updates a dashboard with:

- **Ranking**: every company sorted (highest first) by total orders won in the last 365 days as a % of TTM revenue
- **New orders**: what was announced in the last 1 / 7 / 30 / 90 days, with BIG (≥25% of revenue) and MEANINGFUL (≥10%) tags
- **Company page**: market cap, TTM revenue, order value by month, and every order with its filing link

## One-time setup (about 10 minutes)

1. **Create the repo.** On github.com click **New repository**, name it `order-win-screener`, and keep it **Public** (GitHub Pages is free for public repos). Click **Create repository**.
2. **Upload the files.** On the new repo page click **uploading an existing file**, then drag in everything from this folder (including the `.github` folder). Click **Commit changes**.
   - If the `.github` folder doesn't upload from your phone, do this step on a computer.
3. **Let the job save results.** Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and click **Save**.
4. **Turn on the dashboard.** Go to **Settings → Pages**. Under *Build and deployment*, set Source to **Deploy from a branch**, Branch to **main**, and folder to **/docs**. Click **Save**.
5. **Run the first full scan.** Go to **Actions → Daily order scan → Run workflow**. Set **days** to `365` and **max_pdfs** to `2500`, then click **Run workflow**. This first run takes 1–3 hours.
6. **Open the dashboard** at `https://<your-username>.github.io/order-win-screener/`. Add it to your phone's home screen.

After that it runs by itself every day at about 8:50 pm IST. GitHub can start scheduled jobs late by up to an hour.

## Things to know

- **NSE often blocks GitHub's servers.** BSE works reliably, and almost every company that files on NSE also files the same order on BSE. Check the run log under *Actions* if you see NSE errors.
- **Some filings don't state a value.** Those orders still appear, with a "—" value. Each daily run opens up to 150 more filing PDFs to try to find missing values.
- **Fundamentals come from Yahoo Finance.** Market cap and TTM revenue are refreshed weekly. If a company's revenue is missing, it appears at the bottom of the ranking.
- **Settings** (365-day window, BIG/MEANINGFUL cut-offs, USD rate) are at the top of `screener/core.py`.

## Run it on your own computer

```
pip install -r requirements.txt
python -m screener.run --days 365 --max-pdfs 2500   # first run
python -m screener.run                              # daily
```
Then open `docs/index.html` through a local server: `python -m http.server -d docs`.
