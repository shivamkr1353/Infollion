"""Log Analysis Tool.

This module parses and correlates web server logs and background worker logs
to investigate a production incident affecting checkout processing.

Every statistic published in ANSWERS.md is computed and printed by this
script so that the report is fully reproducible from the raw logs.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# Constants
DEFAULT_WEB_LOG_PATH = Path("web.log")
DEFAULT_WORKER_LOG_PATH = Path("worker.log")

PATH_CHECKOUT = "/checkout"
ASYNC_STATUS = "202"
WORKER_THREAD = "worker"
METRICS_WORKER_THREAD = "metrics-worker"

WEB_LOG_REGEX = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"(?P<level>[A-Z]+) "
    r"\[request\] "
    r"method=(?P<method>[A-Z]+) "
    r"path=(?P<path>\S+) "
    r"status=(?P<status>\d{3}) "
    r"latency_ms=(?P<latency_ms>\d+) "
    r"user_id=(?P<user_id>\d+) "
    r"request_id=(?P<request_id>[a-f0-9]+)$"
)

WORKER_LOG_REGEX = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"(?P<level>[A-Z]+) "
    r"\[(?P<thread>[^\]]+)\] "
    r"(?P<message>.*)$"
)

REQUEST_ID_REGEX = re.compile(r"request_id=(?P<request_id>[a-f0-9]+)")
DURATION_REGEX = re.compile(r"duration_ms=(?P<dur>\d+)")
ERROR_DETAILS_REGEX = re.compile(r"err=(?P<err>\S+) upstream=(?P<upstream>\S+)")
PRODUCT_DETAIL_REGEX = re.compile(r"^/product/\d+$")


def normalize_path(path: str) -> str:
    """Collapse parameterized product detail paths into a single route."""
    if PRODUCT_DETAIL_REGEX.match(path):
        return "/product/:id"
    return path


def parse_web_log(file_path: Path) -> Dict[str, Any]:
    """Parse web server log file.

    Every line is accounted for: request lines are indexed by request_id,
    and non-request lines (deploy events, slow-query warnings, anything
    else) are captured instead of being silently discarded.

    Args:
        file_path: Path to the web.log file.

    Returns:
        Dictionary with request records, non-request lines, and line counts.
    """
    requests: Dict[str, Dict[str, Any]] = {}
    deploy_events: List[str] = []
    slow_query_count = 0
    other_unmatched: List[str] = []
    total_lines = 0

    with file_path.open("r", encoding="utf-8") as file:
        for line in file:
            total_lines += 1
            cleaned_line = line.strip()
            match = WEB_LOG_REGEX.match(cleaned_line)
            if not match:
                if "[deploy]" in cleaned_line:
                    deploy_events.append(cleaned_line)
                elif "[db]" in cleaned_line and "slow query" in cleaned_line:
                    slow_query_count += 1
                else:
                    other_unmatched.append(cleaned_line)
                continue
            data = match.groupdict()
            request_id = data["request_id"]
            if request_id in requests:
                raise ValueError(f"Duplicate request_id in web.log: {request_id}")
            requests[request_id] = {
                "timestamp": datetime.strptime(data["timestamp"], "%Y-%m-%d %H:%M:%S.%f"),
                "timestamp_str": data["timestamp"],
                "method": data["method"],
                "path": data["path"],
                "status": data["status"],
                "user_id": int(data["user_id"]),
            }

    return {
        "requests": requests,
        "deploy_events": deploy_events,
        "slow_query_count": slow_query_count,
        "other_unmatched": other_unmatched,
        "total_lines": total_lines,
    }


def parse_worker_log(file_path: Path) -> Dict[str, Any]:
    """Parse worker log file.

    Job outcomes on the [worker] thread are indexed by request_id.
    [metrics-worker] lines and any unclassified lines are counted
    rather than silently discarded.

    Args:
        file_path: Path to the worker.log file.

    Returns:
        Dictionary with job records, metrics error count, and line counts.
    """
    jobs: Dict[str, Dict[str, Any]] = {}
    metrics_error_count = 0
    other_unmatched: List[str] = []
    total_lines = 0

    with file_path.open("r", encoding="utf-8") as file:
        for line in file:
            total_lines += 1
            cleaned_line = line.strip()
            match = WORKER_LOG_REGEX.match(cleaned_line)
            if not match:
                other_unmatched.append(cleaned_line)
                continue

            data = match.groupdict()
            thread = data["thread"]
            message = data["message"]

            if thread == METRICS_WORKER_THREAD:
                metrics_error_count += 1
                continue
            if thread != WORKER_THREAD:
                other_unmatched.append(cleaned_line)
                continue

            req_id_match = REQUEST_ID_REGEX.search(message)
            if not req_id_match:
                other_unmatched.append(cleaned_line)
                continue
            request_id = req_id_match.group("request_id")
            if request_id in jobs:
                raise ValueError(f"Duplicate request_id in worker.log: {request_id}")

            timestamp = datetime.strptime(data["timestamp"], "%Y-%m-%d %H:%M:%S.%f")

            if message.startswith("job completed"):
                duration_match = DURATION_REGEX.search(message)
                duration = int(duration_match.group("dur")) if duration_match else None
                jobs[request_id] = {
                    "timestamp": timestamp,
                    "timestamp_str": data["timestamp"],
                    "outcome": "completed",
                    "duration_ms": duration,
                    "error": None,
                    "upstream": None,
                }
            elif message.startswith("upstream call failed"):
                err_match = ERROR_DETAILS_REGEX.search(message)
                error_code = err_match.group("err") if err_match else "unknown"
                upstream = err_match.group("upstream") if err_match else "unknown"
                jobs[request_id] = {
                    "timestamp": timestamp,
                    "timestamp_str": data["timestamp"],
                    "outcome": "failed",
                    "duration_ms": None,
                    "error": error_code,
                    "upstream": upstream,
                }
            else:
                other_unmatched.append(cleaned_line)

    return {
        "jobs": jobs,
        "metrics_error_count": metrics_error_count,
        "other_unmatched": other_unmatched,
        "total_lines": total_lines,
    }


def compute_endpoint_stats(
    requests: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Compute per-endpoint method and status code distributions.

    Parameterized paths (e.g. /product/12345) are normalized to a single
    route so that every request line is represented in the table.
    """
    methods: Dict[str, Counter] = defaultdict(Counter)
    statuses: Dict[str, Counter] = defaultdict(Counter)

    for req in requests.values():
        path = normalize_path(req["path"])
        methods[path][req["method"]] += 1
        statuses[path][req["status"]] += 1

    rows = []
    for path in sorted(statuses, key=lambda p: -sum(statuses[p].values())):
        rows.append({
            "path": path,
            "methods": dict(methods[path]),
            "statuses": dict(statuses[path]),
            "total": sum(statuses[path].values()),
        })
    return rows


def run_analysis(web_log_path: Path, worker_log_path: Path) -> Optional[Dict[str, Any]]:
    """Run correlation analysis between web requests and background worker jobs.

    Args:
        web_log_path: Path to the web.log file.
        worker_log_path: Path to the worker.log file.

    Returns:
        Analysis statistics if errors are found, otherwise None.
    """
    web = parse_web_log(web_log_path)
    worker = parse_worker_log(worker_log_path)
    web_requests = web["requests"]
    worker_jobs = worker["jobs"]

    endpoint_stats = compute_endpoint_stats(web_requests)

    # Async queue outcomes per originating path (all 202 endpoints)
    async_outcomes: Dict[str, Counter] = defaultdict(Counter)
    for request_id, req in web_requests.items():
        if req["status"] != ASYNC_STATUS:
            continue
        job = worker_jobs.get(request_id)
        outcome = job["outcome"] if job else "not_queued"
        async_outcomes[req["path"]][outcome] += 1

    checkouts: List[Dict[str, Any]] = []
    for request_id, req in web_requests.items():
        if req["path"] == PATH_CHECKOUT:
            job = worker_jobs.get(request_id)
            checkouts.append({
                "request_id": request_id,
                "web_time": req["timestamp"],
                "user_id": req["user_id"],
                "outcome": job["outcome"] if job else "not_queued",
                "duration_ms": job["duration_ms"] if job else None,
                "error": job["error"] if job else None,
                "worker_time": job["timestamp"] if job else None,
                "upstream": job["upstream"] if job else None,
            })

    checkouts.sort(key=lambda x: x["web_time"])

    failures = [c for c in checkouts if c["outcome"] == "failed"]
    if not failures:
        return None

    first_failure = failures[0]
    last_failure = failures[-1]
    incident_start_time = first_failure["web_time"]

    # Boundary context: checkouts immediately around the first failure
    first_failure_index = checkouts.index(first_failure)
    boundary_rows = checkouts[max(0, first_failure_index - 3):first_failure_index + 3]

    pre_incident = [c for c in checkouts if c["web_time"] < incident_start_time]
    post_incident = [c for c in checkouts if c["web_time"] >= incident_start_time]

    post_even = [c for c in post_incident if c["user_id"] % 2 == 0]
    post_odd = [c for c in post_incident if c["user_id"] % 2 != 0]

    # Failed-attempt distribution per user (computed, never hand-entered)
    failures_per_user = Counter(c["user_id"] for c in failures)
    attempt_distribution = Counter(failures_per_user.values())
    multi_failure_users = {u for u, n in failures_per_user.items() if n > 1}
    completed_users = {c["user_id"] for c in checkouts if c["outcome"] == "completed"}
    multi_failure_users_with_success = multi_failure_users & completed_users

    # Pre-incident completed durations by parity
    def average_duration(records: List[Dict[str, Any]]) -> Optional[float]:
        durations = [c["duration_ms"] for c in records if c["duration_ms"] is not None]
        return round(sum(durations) / len(durations), 1) if durations else None

    pre_odd_completed = [
        c for c in pre_incident if c["user_id"] % 2 != 0 and c["outcome"] == "completed"
    ]
    pre_even_completed = [
        c for c in pre_incident if c["user_id"] % 2 == 0 and c["outcome"] == "completed"
    ]

    failed_upstreams = Counter(c["upstream"] for c in failures if c["upstream"])
    error_codes = Counter(c["error"] for c in failures if c["error"])

    return {
        "web": web,
        "worker": worker,
        "endpoint_stats": endpoint_stats,
        "async_outcomes": {p: dict(c) for p, c in async_outcomes.items()},
        "total_checkouts": len(checkouts),
        "pre_incident_count": len(pre_incident),
        "post_incident_count": len(post_incident),
        "failures_count": len(failures),
        "first_failure": first_failure,
        "last_failure": last_failure,
        "boundary_rows": boundary_rows,
        "post_even_count": len(post_even),
        "post_even_completed": sum(1 for c in post_even if c["outcome"] == "completed"),
        "post_even_failed": sum(1 for c in post_even if c["outcome"] == "failed"),
        "post_odd_count": len(post_odd),
        "post_odd_completed": sum(1 for c in post_odd if c["outcome"] == "completed"),
        "post_odd_failed": sum(1 for c in post_odd if c["outcome"] == "failed"),
        "affected_users_count": len(failures_per_user),
        "attempt_distribution": dict(sorted(attempt_distribution.items())),
        "multi_failure_users_with_success": sorted(multi_failure_users_with_success),
        "pre_odd_avg_duration": average_duration(pre_odd_completed),
        "pre_even_avg_duration": average_duration(pre_even_completed),
        "failed_upstreams": dict(failed_upstreams),
        "error_codes": dict(error_codes),
    }


def display_report(stats: Dict[str, Any]) -> None:
    """Print the final technical analysis report to stdout.

    Args:
        stats: Compiled analysis metrics.
    """
    web = stats["web"]
    worker = stats["worker"]
    first_fail = stats["first_failure"]
    last_fail = stats["last_failure"]

    print("=== INCIDENT ANALYSIS REPORT ===\n")

    print("--- LOG PARSE ACCOUNTING ---")
    print(f"web.log lines:    {web['total_lines']}")
    print(f"  [request] lines parsed: {len(web['requests'])}")
    print(f"  [deploy] events:        {len(web['deploy_events'])}")
    print(f"  [db] slow query WARNs:  {web['slow_query_count']}")
    print(f"  other unmatched lines:  {len(web['other_unmatched'])}")
    for line in web["other_unmatched"][:5]:
        print(f"    ? {line}")
    print(f"worker.log lines: {worker['total_lines']}")
    print(f"  [worker] jobs parsed:      {len(worker['jobs'])}")
    print(f"  [metrics-worker] errors:   {worker['metrics_error_count']}")
    print(f"  other unmatched lines:     {len(worker['other_unmatched'])}")
    for line in worker["other_unmatched"][:5]:
        print(f"    ? {line}")

    print("\n--- DEPLOYMENT EVENTS (web.log) ---")
    for event in web["deploy_events"]:
        print(f"  {event}")

    print("\n--- ENDPOINT STATISTICS (web.log) ---")
    header = f"{'Path':<15} {'Total':>7}  Methods / Statuses"
    print(header)
    for row in stats["endpoint_stats"]:
        methods = ", ".join(f"{m}={n}" for m, n in sorted(row["methods"].items()))
        statuses = ", ".join(f"{s}={n}" for s, n in sorted(row["statuses"].items()))
        print(f"{row['path']:<15} {row['total']:>7}  {methods} | {statuses}")

    print("\n--- ASYNC (202) QUEUE OUTCOMES ---")
    for path, outcomes in sorted(stats["async_outcomes"].items()):
        completed = outcomes.get("completed", 0)
        failed = outcomes.get("failed", 0)
        total = sum(outcomes.values())
        rate = round(failed / total * 100, 2) if total else 0.0
        print(f"  {path:<12} queued={total} completed={completed} "
              f"failed={failed} failure_rate={rate}%")

    print("\n--- INCIDENT BOUNDARY ---")
    print(f"Incident Start Time (Web Log): {first_fail['web_time']}")
    print(f"Incident Start Time (Worker Log): {first_fail['worker_time']}")
    print(f"Earliest Failed Request ID: {first_fail['request_id']}")
    print(f"Last Failed Request ID: {last_fail['request_id']}")
    print(f"Last Failure Time (Web Log): {last_fail['web_time']}")
    print("Checkouts around the boundary:")
    for row in stats["boundary_rows"]:
        parity = "odd" if row["user_id"] % 2 else "even"
        print(f"  {row['web_time']} request_id={row['request_id']} "
              f"user_id={row['user_id']} ({parity}) -> {row['outcome']}")

    print("\n--- VOLUME METRICS ---")
    print(f"Total Checkout Attempts: {stats['total_checkouts']}")
    print(f"Pre-incident Checkout Attempts: {stats['pre_incident_count']}")
    print(f"Post-incident Checkout Attempts: {stats['post_incident_count']}")
    print(f"Total Failed Checkout Jobs: {stats['failures_count']}")
    print(f"Unique Affected Users: {stats['affected_users_count']}")

    print("\n--- FAILED-ATTEMPT DISTRIBUTION (per user) ---")
    for attempts, users in stats["attempt_distribution"].items():
        print(f"  Users with exactly {attempts} failed attempt(s): {users}")
    print(f"  Multi-failure users who also had a completed checkout: "
          f"{len(stats['multi_failure_users_with_success'])}")

    print("\n--- SEGMENTATION METRICS (POST-INCIDENT) ---")
    even_total = stats["post_even_count"]
    odd_total = stats["post_odd_count"]
    even_fail_rate = round(stats["post_even_failed"] / even_total * 100, 2) if even_total else 0.0
    odd_fail_rate = round(stats["post_odd_failed"] / odd_total * 100, 2) if odd_total else 0.0
    odd_success_rate = round(stats["post_odd_completed"] / odd_total * 100, 2) if odd_total else 0.0
    print(f"Even User ID Checkouts: {even_total}")
    print(f"  Completed: {stats['post_even_completed']}")
    print(f"  Failed:    {stats['post_even_failed']} ({even_fail_rate}%)")
    print(f"Odd User ID Checkouts:  {odd_total}")
    print(f"  Completed: {stats['post_odd_completed']} ({odd_success_rate}%)")
    print(f"  Failed:    {stats['post_odd_failed']} ({odd_fail_rate}%)")

    print("\n--- PRE-INCIDENT COMPLETED DURATIONS ---")
    print(f"Odd user average duration_ms:  {stats['pre_odd_avg_duration']}")
    print(f"Even user average duration_ms: {stats['pre_even_avg_duration']}")

    print("\n--- TECHNICAL ERROR SIGNATURE ---")
    print(f"Upstream Destinations: {stats['failed_upstreams']}")
    print(f"Error Code Signatures: {stats['error_codes']}")


if __name__ == "__main__":
    analysis_stats = run_analysis(DEFAULT_WEB_LOG_PATH, DEFAULT_WORKER_LOG_PATH)
    if analysis_stats:
        display_report(analysis_stats)
    else:
        print("No incident failures detected in logs.")
