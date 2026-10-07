"""只读诊断: Codex / Claude CLI 是否可用 (2026-10-08).

背景: 10/7 起 AI 复核每次报 codex_error exit=1, stderr 开头是
"failed to load models cache: unknown variant `max`" (stderr 被截断到 500 字, 看不到真正退出原因).
本脚本: 找出所有 codex 可执行文件 + 版本, 用与系统相同的参数跑一个极短的提示词, 记录完整 stderr;
再测 claude CLI. 结果写 logs/ai_cli_diag_last.json. 不改任何配置文件.
"""
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import ai_prompt

OUT = Path(__file__).resolve().parent / "logs" / "ai_cli_diag_last.json"
PROMPT = "Reply with exactly the two letters OK and nothing else."


def _run(cmd, input_text=None, timeout=120, env=None, cwd=None):
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, cwd=cwd,
                           **ai_prompt._hidden_cli_subprocess_kwargs())
        return {"rc": r.returncode, "secs": round(time.time() - t0, 1),
                "stdout": ai_prompt._redact_cli_text(r.stdout or "")[-3000:],
                "stderr": ai_prompt._redact_cli_text(r.stderr or "")[-6000:]}
    except subprocess.TimeoutExpired:
        return {"rc": None, "secs": timeout, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {"rc": None, "error": f"{type(e).__name__}: {e}"}


def codex_candidates():
    c = []
    known = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if known.exists():
        c.append(str(known))
    for name in ("codex.exe", "codex.cmd", "codex"):
        r = _run(["where", name], timeout=10)
        for line in (r.get("stdout") or "").splitlines():
            if line.strip() and line.strip() not in c:
                c.append(line.strip())
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
    if npm.exists() and str(npm) not in c:
        c.append(str(npm))
    return c


def main():
    res = {"ts": datetime.now(timezone.utc).isoformat(),
           "policy": ai_prompt.get_ai_cli_policy(),
           "selected_codex": ai_prompt._find_codex_cli(),
           "selected_claude": ai_prompt._find_claude_cli(),
           "codex": [], "claude": None}
    for exe in codex_candidates():
        item = {"path": exe, "version": _run([exe, "--version"], timeout=30)}
        with tempfile.TemporaryDirectory(prefix="codex_diag_") as d:
            out = Path(d) / "last.txt"
            cmd = [exe, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--cd", d,
                   "--output-last-message", str(out), "-"]
            item["exec"] = _run(cmd, input_text=PROMPT, timeout=180, env=ai_prompt._codex_safe_env(), cwd=d)
            try:
                item["exec"]["last_message"] = out.read_text(encoding="utf-8", errors="replace")[:200]
            except Exception:
                item["exec"]["last_message"] = None
        res["codex"].append(item)
    cl = ai_prompt._find_claude_cli()
    if cl:
        res["claude"] = {"path": cl, "version": _run([cl, "--version"], timeout=30),
                         "exec": _run([cl, "-p"], input_text=PROMPT, timeout=180)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
