#!/usr/bin/env python3
"""Solução de referência para task_tb_log_summary — lê logs/, escreve summary.csv.
Rodar no diretório do workspace (após copiar fixtures/logs)."""

import csv
import re
from datetime import datetime, timedelta
from pathlib import Path

CURRENT_DATE = datetime(2025, 8, 12)
SEVERITIES = ("ERROR", "WARNING", "INFO")


def period_ranges(today: datetime):
    return {
        "today": (today, today),
        "last_7_days": (today - timedelta(days=6), today),
        "last_30_days": (today - timedelta(days=29), today),
        "month_to_date": (today.replace(day=1), today),
    }


def main() -> None:
    logs_dir = Path("logs")
    ranges = period_ranges(CURRENT_DATE)
    counts = {p: {s: 0 for s in SEVERITIES} for p in ranges}
    total = {s: 0 for s in SEVERITIES}

    for log_file in logs_dir.glob("*.log"):
        date_str = log_file.name.split("_")[0]
        file_date = datetime.strptime(date_str, "%Y-%m-%d")
        text = log_file.read_text()
        for sev in SEVERITIES:
            n = len(re.findall(rf"\[{sev}\]", text))
            total[sev] += n
            for period, (start, end) in ranges.items():
                if start <= file_date <= end:
                    counts[period][sev] += n

    with open("summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["period", "severity", "count"])
        for period in ("today", "last_7_days", "last_30_days", "month_to_date"):
            for sev in SEVERITIES:
                w.writerow([period, sev, counts[period][sev]])
        for sev in SEVERITIES:
            w.writerow(["total", sev, total[sev]])


if __name__ == "__main__":
    main()
