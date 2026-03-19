# 🔍 famous-roblox-users-finder

A fast, production-grade CLI tool that takes a Roblox seller/purchaser CSV report and identifies **"famous" users** — those who exceed a configurable follower threshold. Built for game developers and marketplace sellers who want to spot high-profile buyers in bulk, without manual lookups.

---

## ✨ Features

- **Concurrent scanning** — scans multiple users in parallel via a configurable thread pool
- **Exponential backoff with jitter** — automatically retries failed API calls without hammering the endpoint
- **Live progress bar** — real-time feedback via `tqdm` (degrades gracefully if not installed)
- **Deduplication** — skips repeated user IDs before scanning, saving time and requests
- **Two output reports** — a focused famous-users CSV and a full merged results CSV
- **Structured logging** — logs to both console and a `.log` file simultaneously
- **Graceful Ctrl+C handling** — partial results are saved if you interrupt mid-scan
- **Dry-run mode** — preview what would be scanned without making any API requests
- **Fully configurable CLI** — every parameter is a flag; no need to edit the source

---

## 📋 Requirements

- Python **3.8+**
- Dependencies listed in [`requirements.txt`](requirements.txt)

Install dependencies with:

```bash
pip install -r requirements.txt
```

---

## 🚀 Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/your-username/famous-roblox-users-finder.git
cd famous-roblox-users-finder

# 2. Install dependencies
pip install -r requirements.txt

# 3. Place your CSV in the data/ folder
mkdir -p data
cp /path/to/sellerReport.csv data/sellerReport.csv

# 4. Run the scanner
python roblox_follower_scanner.py
```

---

## 📂 Project Structure

```
famous-roblox-users-finder/
├── roblox_follower_scanner.py   # Main script
├── requirements.txt             # Python dependencies
├── .gitignore                   # Files excluded from git
├── data/
│   └── sellerReport.csv         # ← Put your input CSV here
└── README.md
```

---

## 🖥️ How It Works

```
Input CSV
  └─► Deduplicate user IDs
        └─► ThreadPoolExecutor (N workers)
              └─► GET /v1/users/{id}/followers/count  (roproxy)
                    ├─ Success  → record follower count
                    └─ Failure  → exponential backoff → retry up to N times
                          │
                          ▼
              ┌─────────────────────────────┐
              │   follower_count >= threshold│
              │   → write to famous_users.csv│
              └─────────────────────────────┘
                          │
                          ▼
              full_results.csv  (all users + original columns merged)
              scanner.log       (full debug log)
```

1. The script reads your input CSV and extracts all values from the `Purchaser Id` column (configurable).
2. User IDs are deduplicated so each Roblox account is only looked up once.
3. A thread pool fires off concurrent requests to the [RoProxy](https://roproxy.com) Roblox API mirror.
4. Each request uses exponential backoff with random jitter on failure — starting at 1s, doubling each retry, up to 8 attempts by default.
5. HTTP 404 responses (deleted/invalid accounts) are short-circuited immediately without retrying.
6. Famous users (≥ threshold followers) are written to `famous_users.csv` in real time as they're found.
7. Once all users are scanned, the full results are merged back with your original CSV columns and saved to `full_results.csv`.

---

## ⚙️ CLI Options

Run `python roblox_follower_scanner.py --help` to see all options:

| Flag                | Default                 | Description                                 |
| ------------------- | ----------------------- | ------------------------------------------- |
| `--input`           | `data/sellerReport.csv` | Path to the input CSV file                  |
| `--purchaser-col`   | `Purchaser Id`          | Column name containing Roblox user IDs      |
| `--threshold`       | `5000`                  | Minimum followers to be considered "famous" |
| `--output-famous`   | `famous_users.csv`      | Output path for the famous-users report     |
| `--output-full`     | `full_results.csv`      | Output path for the full merged results     |
| `--log-file`        | `scanner.log`           | Path to the log file                        |
| `--workers`         | `4`                     | Number of concurrent threads                |
| `--max-attempts`    | `8`                     | Max retries per user on API failure         |
| `--base-wait`       | `1.0`                   | Initial backoff wait in seconds             |
| `--rate-limit-wait` | `0.15`                  | Pause between requests per thread (seconds) |
| `--verbose` / `-v`  | off                     | Enable verbose debug logging                |
| `--dry-run`         | off                     | Preview scan without making API requests    |

### Examples

```bash
# Default run
python roblox_follower_scanner.py

# Custom input file and higher threshold
python roblox_follower_scanner.py --input data/myReport.csv --threshold 10000

# Faster scan with more workers
python roblox_follower_scanner.py --workers 8

# Use a different column name for user IDs
python roblox_follower_scanner.py --purchaser-col "User ID"

# Preview without making requests
python roblox_follower_scanner.py --dry-run

# Verbose debug output
python roblox_follower_scanner.py -v
```

---

## 📊 Output Files

After a successful run, three files are produced:

### `famous_users.csv`

Contains only users who met or exceeded the follower threshold. Written **in real time** as the scan progresses — so you can open it while the script is still running.

```
UserID,Followers
1234567,82400
8901234,15200
...
```

### `full_results.csv`

All original rows from your input CSV, with four new columns appended:

| Column      | Description                                         |
| ----------- | --------------------------------------------------- |
| `Followers` | Follower count returned by the API                  |
| `IsFamous`  | `True` if the user met the threshold                |
| `Attempts`  | How many API attempts were needed                   |
| `Error`     | Error message if the lookup failed, otherwise empty |

### `scanner.log`

Full debug log of the entire run — including retry attempts, timing, and the final summary. Useful for diagnosing failures or auditing a scan.

---

## 📝 Input CSV Format

Your input CSV must have a column containing Roblox user IDs. By default the script looks for a column named `Purchaser Id` — this matches the export format from the Roblox marketplace seller dashboard. If your column is named differently, pass `--purchaser-col "Your Column Name"`.

Example input:

```
Transaction Id,Purchaser Id,Item Name,Price
TXN001,1234567,Cool Sword,50
TXN002,8901234,Red Hat,25
TXN003,1234567,Blue Shirt,10
```

The duplicate `1234567` in the example above will be scanned only once.

---

## ⚠️ Notes & Limitations

- Note that most of the people with high amount of followers are botted, so not all of them are really famous
- This tool uses [RoProxy](https://roproxy.com), a public Roblox API proxy. It is not affiliated with or endorsed by Roblox Corporation.
- Be mindful of rate limits. The default `--rate-limit-wait 0.15` and `--workers 4` settings are conservative. Increasing workers aggressively may trigger rate limiting.
- Roblox accounts that have been deleted or banned return a 404 and are skipped gracefully.
- This tool reads **public** follower counts only. No authentication is required.

---

## 📄 License

MIT — see [`LICENSE`](LICENSE) for details.
