"""_codex_update.py — 2026-10-15: 升级本机 Codex CLI (交易 AI 的备用通道).

背景: 本机 AppData\\Local\\OpenAI\\Codex\\bin\\codex.exe (0.130.0-alpha.5) 太旧,
读不了服务端新返回的模型列表 (unknown variant `max`), 每次都 exit=1; Claude 周额度用完时
交易 AI 复核就完全不可用 (fail-closed 不下单).
本脚本只做: 记录现状 → npm 全局安装最新 @openai/codex → 记录新版本和登录状态.
不改任何配置/代码. 结果: logs/codex_update_last.json
"""
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "logs" / "codex_update_last.json"
HIDDEN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def run(cmd, timeout=300, input_text=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
                           encoding="utf-8", errors="replace", shell=False, **HIDDEN)
        return {"cmd": " ".join(map(str, cmd)), "rc": r.returncode,
                "stdout": (r.stdout or "")[-2500:], "stderr": (r.stderr or "")[-2500:]}
    except Exception as e:  # noqa: BLE001
        return {"cmd": " ".join(map(str, cmd)), "error": f"{type(e).__name__}: {e}"}


def main():
    res = {"ts": datetime.now(timezone.utc).isoformat()}
    standalone = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    npm_codex = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    res["npm"] = npm
    res["before"] = {"standalone": run([str(standalone), "--version"], 30) if standalone.exists() else None,
                     "npm_codex": run([str(npm_codex), "--version"], 30) if npm_codex.exists() else None}
    if npm:
        res["install"] = run([npm, "install", "-g", "@openai/codex@latest"], timeout=600)
    else:
        res["install"] = {"error": "npm not found (Node.js 未安装)"}
    res["after"] = {"npm_codex": run([str(npm_codex), "--version"], 30) if npm_codex.exists() else None}
    if npm_codex.exists():
        res["login_status"] = run([str(npm_codex), "login", "status"], 60)
        res["smoke"] = run([str(npm_codex), "exec", "--skip-git-repo-check", "回复 OK 两个字母"], 180)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
