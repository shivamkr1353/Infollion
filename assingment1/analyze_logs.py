"""Log Analysis Tool.

This module parses and correlates web server logs and background worker logs
to investigate a production incident affecting checkout processing.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Any, Optional

# Constants
DEFAULT_WEB_LOG_PATH = Path("web.log")
DEFAULT_WORKER_LOG_PATH = Path("worker.log")

PATH_CHECKOUT = "/checkout"
STATUS_ACCEPTED = "202"
WORKER_THREAD_IDENTIFIER = "[worker]"
METRICS_WORKER_THREAD_IDENTIFIER = "[metrics-worker]"

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
ERROR_DETAILS_REGEX = re.compile(r"err=(?P<err>\S+) upstream=(?P<upstream>\S+)")


def parse_web_log(file_path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse web server log file and index requests by request_id.

    Args:
        file_path: Path to the web.log file.

    Returns:
        Dictionary mapping request_id to request details.
    """
    requests: Dict[str, Dict[str, Any]] = {}
    with file_path.open("r", encoding="utf-8") as file:
        for line in file:
            match = WEB_LOG_REGEX.match(line.strip())
            if not match:
                continue
            data = match.groupdict()
            request_id = data["request_id"]
            requests[request_id] = {
                "timestamp": datetime.strptime(data["timestamp"], "%Y-%m-%d %H:%M:%S.%f"),
                "timestamp_str": data["timestamp"],
                "method": data["method"],
                "path": data["path"],
                "status": data["status"],
                "user_id": int(data["user_id"]),
            }
    return requests


def parse_worker_log(file_path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse worker log file and index background jobs by request_id.

    Args:
        file_path: Path to the worker.log file.

    Returns:
        Dictionary mapping request_id to job processing status and logs.
    """
    jobs: Dict[str, Dict[str, Any]] = {}
    with file_path.open("r", encoding="utf-8") as file:
        for line in file:
            cleaned_line = line.strip()
            if WORKER_THREAD_IDENTIFIER not in cleaned_line:
                continue

            match = WORKER_LOG_REGEX.match(cleaned_line)
            if not match:
                continue

            data = match.groupdict()
            message = data["message"]

            req_id_match = REQUEST_ID_REGEX.search(message)
            if not req_id_match:
                continue
            request_id = req_id_match.group("request_id")

            timestamp = datetime.strptime(data["timestamp"], "%Y-%m-%d %H:%M:%S.%f")

            if "job completed" in message:
                duration_match = re.search(r"duration_ms=(?P<dur>\d+)", message)
                duration = int(duration_match.group("dur")) if duration_match else None
                jobs[request_id] = {
                    "timestamp": timestamp,
                    "timestamp_str": data["timestamp"],
                    "outcome": "completed",
                    "duration_ms": duration,
                    "error": None,
                    "upstream": None,
                }
            elif "failed" in message:
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
    return jobs


def run_analysis(
    web_log_path: Path, worker_log_path: Path
) -> Optional[Dict[str, Any]]:
    """Run correlation analysis between web requests and background worker jobs.

    Args:
        web_log_path: Path to the web.log file.
        worker_log_path: Path to the worker.log file.

    Returns:
        Analysis statistics if errors are found, otherwise None.
    """
    web_requests = parse_web_log(web_log_path)
    worker_jobs = parse_worker_log(worker_log_path)

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

    pre_incident = [c for c in checkouts if c["web_time"] < incident_start_time]
    post_incident = [c for c in checkouts if c["web_time"] >= incident_start_time]

    post_even = [c for c in post_incident if c["user_id"] % 2 == 0]
    post_odd = [c for c in post_incident if c["user_id"] % 2 != 0]

    affected_users = {c["user_id"] for c in failures}

    # Upstream and error stats
    failed_upstreams: Set[str] = {c["upstream"] for c in failures if c["upstream"]}
    error_codes: Set[str] = {c["error"] for c in failures if c["error"]}

    return {
        "total_checkouts": len(checkouts),
        "pre_incident_count": len(pre_incident),
        "post_incident_count": len(post_incident),
        "failures_count": len(failures),
        "first_failure": first_failure,
        "last_failure": last_failure,
        "post_even_count": len(post_even),
        "post_even_completed": sum(1 for c in post_even if c["outcome"] == "completed"),
        "post_even_failed": sum(1 for c in post_even if c["outcome"] == "failed"),
        "post_odd_count": len(post_odd),
        "post_odd_completed": sum(1 for c in post_odd if c["outcome"] == "completed"),
        "post_odd_failed": sum(1 for c in post_odd if c["outcome"] == "failed"),
        "affected_users_count": len(affected_users),
        "failed_upstreams": list(failed_upstreams),
        "error_codes": list(error_codes),
    }


def display_report(stats: Dict[str, Any]) -> None:
    """Print the final technical analysis report to stdout.

    Args:
        stats: Compiled analysis metrics.
    """
    first_fail = stats["first_failure"]
    last_fail = stats["last_failure"]

    print("=== INCIDENT ANALYSIS REPORT ===")
    print(f"Incident Start Time (Web Log): {first_fail['web_time']}")
    print(f"Incident Start Time (Worker Log): {first_fail['worker_time']}")
    print(f"Earliest Failed Request ID: {first_fail['request_id']}")
    print(f"Last Failed Request ID: {last_fail['request_id']}")
    print(f"Last Failure Time (Web Log): {last_fail['web_time']}\n")

    print("--- VOLUME METRICS ---")
    print(f"Total Checkout Attempts: {stats['total_checkouts']}")
    print(f"Pre-incident Checkout Attempts: {stats['pre_incident_count']}")
    print(f"Post-incident Checkout Attempts: {stats['post_incident_count']}")
    print(f"Total Failed Checkout Jobs: {stats['failures_count']}")
    print(f"Unique Affected Users: {stats['affected_users_count']}\n")

    print("--- SEGMENTATION METRICS (POST-INCIDENT) ---")
    print(f"Even User ID Checkouts: {stats['post_even_count']}")
    print(f"  Completed: {stats['post_even_completed']}")
    print(f"  Failed:    {stats['post_even_failed']}")
    print(f"Odd User ID Checkouts:  {stats['post_odd_count']}")
    print(f"  Completed: {stats['post_odd_completed']}")
    print(f"  Failed:    {stats['post_odd_failed']}\n")

    print("--- TECHNICAL ERROR SIGNATURE ---")
    print(f"Upstream Destinations: {stats['failed_upstreams']}")
    print(f"Error Code Signatures: {stats['error_codes']}")


if __name__ == "__main__":
    analysis_stats = run_analysis(DEFAULT_WEB_LOG_PATH, DEFAULT_WORKER_LOG_PATH)
    if analysis_stats:
        display_report(analysis_stats)
    else:
        print("No incident failures detected in logs.")
