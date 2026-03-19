"""
Roblox Follower Scanner
=======================
Scans a CSV report of Roblox purchasers and identifies "famous" users
based on follower count thresholds.

Features:
  - Exponential backoff with jitter on API errors
  - Configurable via CLI arguments or config at the top of the file
  - Live progress bar via tqdm
  - Concurrent requests (configurable worker pool)
  - Detailed logging to file + console
  - Summary report at the end
  - Graceful Ctrl+C handling (saves partial results)
  - Deduplication of user IDs before scanning
  - Dry-run mode (prints what would happen without making requests)
"""

import argparse
import csv
import logging
import random
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# ──────────────────────────────────────────────────────────────
# DEFAULT CONFIG  (override with CLI flags)
# ──────────────────────────────────────────────────────────────
DEFAULT_INPUT_CSV       = "data/sellerReport.csv"
DEFAULT_PURCHASER_COL   = "Purchaser Id"
DEFAULT_FAMOUS_THRESHOLD = 5_000          # min followers to be "famous"
DEFAULT_OUTPUT_FAMOUS   = "output/famous_users.csv"
DEFAULT_OUTPUT_FULL     = "output/full_results.csv"
DEFAULT_LOG_FILE        = "output/logs/scanner.log"
DEFAULT_MAX_WORKERS     = 4               # concurrent threads
DEFAULT_MAX_ATTEMPTS    = 8              # retries per user
DEFAULT_BASE_WAIT       = 1.0            # seconds, doubles each retry
DEFAULT_RATE_LIMIT_WAIT = 0.15           # seconds between requests per thread

API_URL = "https://friends.roproxy.com/v1/users/{user_id}/followers/count"


# ──────────────────────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────────────────────
def setup_logging(log_file: str, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("scanner")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler (always DEBUG level)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ──────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    user_id: int
    follower_count: Optional[int]
    is_famous: bool
    attempts: int
    error: Optional[str] = None


@dataclass
class ScanStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    famous: int = 0
    skipped_duplicates: int = 0
    elapsed_seconds: float = 0.0
    results: list = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# API FETCHER
# ──────────────────────────────────────────────────────────────
def get_follower_count(
    user_id: int,
    session: requests.Session,
    logger: logging.Logger,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_wait: float = DEFAULT_BASE_WAIT,
    rate_limit_wait: float = DEFAULT_RATE_LIMIT_WAIT,
) -> ScanResult:
    """
    Fetch follower count for a Roblox user ID.
    Uses exponential backoff with jitter on failures.
    Returns a ScanResult with count or error info.
    """
    url = API_URL.format(user_id=user_id)
    wait_time = base_wait

    for attempt in range(1, max_attempts + 1):
        try:
            time.sleep(rate_limit_wait)
            response = session.get(url, timeout=10)
            response.raise_for_status()
            count = response.json().get("count")

            if count is None:
                raise ValueError("API returned no 'count' field")

            logger.debug(f"  user {user_id}: {count:,} followers (attempt {attempt})")
            return ScanResult(user_id=user_id, follower_count=count, is_famous=False, attempts=attempt)

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            logger.warning(f"  HTTP {status} for user {user_id} (attempt {attempt}/{max_attempts})")
            # 404 = user not found, no point retrying
            if status == 404:
                return ScanResult(user_id=user_id, follower_count=None, is_famous=False,
                                  attempts=attempt, error="User not found (404)")
        except Exception as e:
            logger.warning(f"  Error for user {user_id} (attempt {attempt}/{max_attempts}): {e}")

        if attempt < max_attempts:
            jitter = random.uniform(0, wait_time * 0.3)
            sleep_for = wait_time + jitter
            logger.debug(f"  Retrying user {user_id} in {sleep_for:.1f}s...")
            time.sleep(sleep_for)
            wait_time *= 2  # exponential backoff

    logger.error(f"  ❌ Gave up on user {user_id} after {max_attempts} attempts")
    return ScanResult(user_id=user_id, follower_count=None, is_famous=False,
                      attempts=max_attempts, error="Max retries exceeded")


# ──────────────────────────────────────────────────────────────
# MAIN SCANNER
# ──────────────────────────────────────────────────────────────
def run_scan(args: argparse.Namespace, logger: logging.Logger) -> ScanStats:
    stats = ScanStats()
    interrupted = False

    # ── Load CSV ──────────────────────────────────────────────
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    df = pd.read_csv(input_path)
    logger.info(f"Loaded {len(df):,} rows from '{input_path}'")

    if args.purchaser_col not in df.columns:
        logger.error(f"Column '{args.purchaser_col}' not found. Available columns: {list(df.columns)}")
        sys.exit(1)

    # ── Deduplicate ───────────────────────────────────────────
    all_ids = df[args.purchaser_col].dropna().astype(int).tolist()
    unique_ids = list(dict.fromkeys(all_ids))  # preserve order, remove dupes
    stats.skipped_duplicates = len(all_ids) - len(unique_ids)
    stats.total = len(unique_ids)

    logger.info(f"Unique user IDs to scan: {stats.total:,}  "
                f"(skipped {stats.skipped_duplicates} duplicates)")
    logger.info(f"Famous threshold: ≥ {args.threshold:,} followers")

    if args.dry_run:
        logger.info("DRY RUN — no API requests will be made.")
        for uid in unique_ids[:5]:
            logger.info(f"  Would scan user_id: {uid}")
        if len(unique_ids) > 5:
            logger.info(f"  ... and {len(unique_ids) - 5} more")
        return stats

    # ── Output files ──────────────────────────────────────────
    famous_path = Path(args.output_famous)
    full_path = Path(args.output_full)

    famous_file = open(famous_path, "w", newline="", encoding="utf-8", buffering=1)
    famous_writer = csv.writer(famous_file)
    famous_writer.writerow(["UserID", "Followers"])

    # ── Graceful interrupt ────────────────────────────────────
    def handle_interrupt(sig, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            logger.warning("\n⚠️  Interrupted! Saving partial results and exiting...")

    signal.signal(signal.SIGINT, handle_interrupt)

    # ── Scanning loop ─────────────────────────────────────────
    session = requests.Session()
    session.headers.update({"User-Agent": "RobloxFollowerScanner/2.0"})

    start_time = time.time()
    completed = 0

    try:
        # Try importing tqdm for a nice progress bar; gracefully degrade if absent
        from tqdm import tqdm
        id_iter = tqdm(unique_ids, desc="Scanning", unit="user", ncols=80)
    except ImportError:
        id_iter = unique_ids
        logger.info("Tip: install tqdm for a live progress bar  →  pip install tqdm")

    def scan_user(uid: int) -> ScanResult:
        return get_follower_count(
            user_id=uid,
            session=session,
            logger=logger,
            max_attempts=args.max_attempts,
            base_wait=args.base_wait,
            rate_limit_wait=args.rate_limit_wait,
        )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(scan_user, uid): uid for uid in unique_ids}

        for future in as_completed(futures):
            if interrupted:
                executor.shutdown(wait=False, cancel_futures=True)
                break

            result = future.result()
            completed += 1
            stats.results.append(result)

            if result.follower_count is not None:
                stats.success += 1
                result.is_famous = result.follower_count >= args.threshold
                if result.is_famous:
                    stats.famous += 1
                    famous_writer.writerow([result.user_id, result.follower_count])
                    famous_file.flush()
                    logger.info(f"⭐ Famous! user {result.user_id:>12,} — {result.follower_count:>10,} followers")
            else:
                stats.failed += 1

            # Periodic progress log (if no tqdm)
            if completed % 50 == 0:
                logger.info(f"Progress: {completed}/{stats.total} scanned "
                            f"({stats.famous} famous so far)")

    famous_file.close()
    stats.elapsed_seconds = time.time() - start_time

    # ── Build full results CSV ────────────────────────────────
    results_df = pd.DataFrame([
        {
            "UserID": r.user_id,
            "Followers": r.follower_count,
            "IsFamous": r.is_famous,
            "Attempts": r.attempts,
            "Error": r.error or "",
        }
        for r in stats.results
    ])

    # Merge back into original df so all original columns are preserved
    original_cols = df.copy()
    original_cols["_uid_"] = original_cols[args.purchaser_col].astype(int)
    results_df = results_df.rename(columns={"UserID": "_uid_"})
    merged = original_cols.merge(results_df, on="_uid_", how="left").drop(columns=["_uid_"])
    merged.to_csv(full_path, index=False)

    return stats


# ──────────────────────────────────────────────────────────────
# SUMMARY
# ──────────────────────────────────────────────────────────────
def print_summary(stats: ScanStats, args: argparse.Namespace, logger: logging.Logger):
    elapsed = stats.elapsed_seconds
    rate = stats.success / elapsed if elapsed > 0 else 0
    sep = "─" * 50

    logger.info(sep)
    logger.info("SCAN COMPLETE — SUMMARY")
    logger.info(sep)
    logger.info(f"  Total unique users scanned : {stats.total:>8,}")
    logger.info(f"  Duplicates skipped         : {stats.skipped_duplicates:>8,}")
    logger.info(f"  Successful lookups         : {stats.success:>8,}")
    logger.info(f"  Failed lookups             : {stats.failed:>8,}")
    logger.info(f"  Famous users (≥{args.threshold:,})    : {stats.famous:>8,}")
    logger.info(f"  Elapsed time               : {elapsed:>8.1f}s")
    logger.info(f"  Avg speed                  : {rate:>8.1f} users/sec")
    logger.info(sep)
    logger.info(f"  → Famous users saved to    : {args.output_famous}")
    logger.info(f"  → Full results saved to    : {args.output_full}")
    logger.info(f"  → Log saved to             : {args.log_file}")
    logger.info(sep)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan Roblox purchaser CSV and find users with many followers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input",          default=DEFAULT_INPUT_CSV,       help="Path to input CSV")
    p.add_argument("--purchaser-col",  default=DEFAULT_PURCHASER_COL,   help="Column name with user IDs")
    p.add_argument("--threshold",      default=DEFAULT_FAMOUS_THRESHOLD, type=int,
                   help="Minimum followers to be considered 'famous'")
    p.add_argument("--output-famous",  default=DEFAULT_OUTPUT_FAMOUS,   help="Output CSV for famous users")
    p.add_argument("--output-full",    default=DEFAULT_OUTPUT_FULL,     help="Output CSV with all results merged")
    p.add_argument("--log-file",       default=DEFAULT_LOG_FILE,        help="Log file path")
    p.add_argument("--workers",        default=DEFAULT_MAX_WORKERS,     type=int,
                   help="Number of concurrent threads")
    p.add_argument("--max-attempts",   default=DEFAULT_MAX_ATTEMPTS,    type=int,
                   help="Max retries per user on API failure")
    p.add_argument("--base-wait",      default=DEFAULT_BASE_WAIT,       type=float,
                   help="Initial backoff wait (seconds)")
    p.add_argument("--rate-limit-wait",default=DEFAULT_RATE_LIMIT_WAIT, type=float,
                   help="Pause between requests per thread (seconds)")
    p.add_argument("--verbose", "-v",  action="store_true",             help="Enable debug logging")
    p.add_argument("--dry-run",        action="store_true",             help="Preview scan without making requests")
    return p


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────
def main():
    args = build_parser().parse_args()
    logger = setup_logging(args.log_file, args.verbose)

    logger.info("=" * 50)
    logger.info("  Roblox Follower Scanner")
    logger.info("=" * 50)

    stats = run_scan(args, logger)
    print_summary(stats, args, logger)


if __name__ == "__main__":
    main()