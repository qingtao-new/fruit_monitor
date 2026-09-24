"""定时 git 存档：把工作区里已入库范围的改动自动提交一次。

由 Windows 计划任务每 30 分钟调用（见 docs/quickstart.md）。

规则：
- 只提交 .gitignore 允许的东西；data/、*.log、_*.py 诊断脚本本来就被挡在外面。
- 提交前扫一遍暂存内容，命中明文凭据就中止，把改动留在工作区等人处理。
  上一次手动提交时就是靠这条把固件里的 WiFi 密码拦下来的。
- 没有改动就什么都不做，不会产生空提交。
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "logs" / "archive.log"

# 命中任意一条就中止提交。占位符和本地配置不算凭据。
# 引号可有可无：C++ 是 PASSWORD = "..."，JSON 是 "password": "..."。
CREDENTIAL_PATTERNS = [
    r'["\']?(?:WIFI_PASSWORD|PASSWORD|PASSWD)["\']?\s*=\s*["\']([^"<][^"\']{3,})["\']',
    r'["\']?(?:password|passwd|pwd)["\']?\s*[:=]\s*["\']([^"<][^"\']{3,})["\']',
    r'["\']?(?:secret|client_secret)["\']?\s*[:=]\s*["\']([^"<][^"\']{8,})["\']',
    r'["\']?(?:api[_-]?key|access[_-]?token)["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{16,})["\']',
    r'gh[pousr]_[A-Za-z0-9]{20,}',
    r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----',
]
PLACEHOLDERS = {"", "null", "none", "true", "false", "your_wifi_password",
                "your_wifi_ssid", "changeme", "xxx", "password", "<your password>"}


def run(args: list[str]) -> str:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def note(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}  {message}\n")


def staged_credentials() -> list[str]:
    """暂存内容里长得像明文凭据的行。"""
    diff = run(["git", "diff", "--cached", "-U0"])
    hits: list[str] = []
    for pattern in CREDENTIAL_PATTERNS:
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                # gh*_ / PRIVATE KEY 那两条没有捕获组，整体就是凭据本身。
                value = (match.group(1) if match.groups() else match.group(0))
                value = value.strip().strip('"').strip("'")
                if value.lower() in PLACEHOLDERS:
                    continue
                hits.append(line.strip()[:160])
    return hits


def main() -> int:
    if not (REPO / ".git").exists():
        note("skip: not a git repo")
        return 1

    status = run(["git", "status", "--porcelain"])
    if not status:
        note("skip: nothing to commit")
        return 0

    run(["git", "add", "-A"])

    staged = run(["git", "diff", "--cached", "--name-only"])
    if not staged:
        note("skip: nothing staged")
        return 0

    secrets = staged_credentials()
    if secrets:
        run(["git", "reset"])
        note("ABORT: credential-looking lines in staged content: "
             + " | ".join(secrets[:5]))
        return 2

    files = [line for line in staged.splitlines() if line]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = f"chore(archive): 自动存档 {stamp} ({len(files)} files)"
    result = subprocess.run(["git", "commit", "-m", message], cwd=REPO,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        note("commit failed: " + (result.stdout + result.stderr)[:300])
        return 3

    revision = run(["git", "rev-parse", "--short", "HEAD"])
    note(f"committed {revision}  {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
