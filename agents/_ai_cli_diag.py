"""只读诊断: Codex / Claude CLI 是否可用 (2026-10-08).

背景: 10/7 起 AI 复核每次报 codex_error exit=1, stderr 开头是
"failed to load models cache: unknown variant `max`" (stderr 被截断到 500 字, 看不到真正退出原因).
本脚本: 找出所有 codex 可执行文件 + 版本, 用与系统相同的参数跑一个极短的提示词, 记录完整 stderr;
再测 claude CLI. 结果写 logs/ai_cli_diag_last.json. 不改任何配置文件.

EVO-20261015-1 补强: 把两个通道的探测结果归一到 (ok|unavailable + failure_class),
写进 signals/ai_gate_health.json (与 claude_gate 共用一份), 并且探测到实际成功
恢复时推一次 recovery 告警 — 恢复必须由本脚本触发, 普通业务路径不发恢复告警.
"""
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import ai_prompt
import claude_gate

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


def _probe_status(exec_result: dict | None, last_message: str | None = None) -> tuple[bool, str]:
    """从 _run 的结果判断一次 CLI 探测是否算真实成功.

    成功判据: rc==0 且输出里包含 PROMPT 要求的 'OK' (大小写忽略, 允许前后空白).
    其它一律视为失败, status 字符串尽量保留 rc + stderr 片段供事后追溯.
    """
    if not isinstance(exec_result, dict):
        return False, "no_exec_result"
    if exec_result.get("error") == "timeout":
        return False, "timeout"
    err = exec_result.get("error")
    if err:
        return False, f"error: {err}"
    rc = exec_result.get("rc")
    text = (last_message or "") or (exec_result.get("stdout") or "")
    if rc == 0 and "ok" in (text or "").strip().lower():
        return True, "ok"
    tail = (exec_result.get("stderr") or exec_result.get("stdout") or "")[-240:]
    return False, f"exit={rc} stderr={tail.strip()}"


def _emit_probe_outcome(channel: str, ok: bool, status: str) -> dict:
    """探测结果写入 ai_gate_health.json; 若由不可用恢复则推 recovery 告警.

    返回 {ok, status, failure_class(if any), recovered, duration_s}.
    """
    if ok:
        info = claude_gate.record_cli_success(channel, probe=True)
        if info.get("recovered"):
            try:
                from notifications import notify_ai_recovery
                notify_ai_recovery(
                    channel=info["channel"],
                    duration_s=info.get("previous_duration_s", 0),
                    classification=info.get("previous_failure_class") or "",
                    overall=info.get("overall"),
                )
            except Exception:
                pass
        return {
            "channel": info["channel"],
            "ok": True,
            "status": status,
            "recovered": info.get("recovered", False),
            "previous_duration_s": info.get("previous_duration_s", 0),
            "overall": info.get("overall"),
        }
    failure_class = claude_gate.classify_cli_failure(status)
    info = claude_gate.record_cli_failure(channel, status, failure_class)
    return {
        "channel": info["channel"],
        "ok": False,
        "status": status,
        "failure_class": failure_class,
        "duration_s": info.get("duration_s", 0),
        "is_new_failure": info.get("is_new_failure", False),
        "overall": info.get("overall"),
    }


def main():
    res = {"ts": datetime.now(timezone.utc).isoformat(),
           "policy": ai_prompt.get_ai_cli_policy(),
           "selected_codex": ai_prompt._find_codex_cli(),
           "selected_claude": ai_prompt._find_claude_cli(),
           "codex": [], "claude": None}
    codex_probe_ok = False
    codex_probe_status = "no_probe"
    for exe in (codex_candidates() if os.environ.get("DIAG_SKIP_CODEX") != "1" else []):
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
        # EVO-20261015-1: 多个 codex 候选, 任一探测成功就算通道健康
        ok, status = _probe_status(item.get("exec"), item.get("exec", {}).get("last_message"))
        item["probe_ok"] = ok
        item["probe_status"] = status
        if ok:
            codex_probe_ok = True
            codex_probe_status = status
        elif not codex_probe_ok:
            codex_probe_status = status
        res["codex"].append(item)
    cl = ai_prompt._find_claude_cli()
    claude_probe_ok = False
    claude_probe_status = "not_installed"
    if cl:
        res["claude_help"] = _run([cl, "--help"], timeout=30)   # 2026-10-12: 自我进化需要确认可用参数
        try:   # _run 只保留尾部 3000 字符；完整 help 另存一份供核对参数
            _h = subprocess.run([cl, "--help"], capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
            (Path(__file__).parent / "logs" / "claude_help_full.txt").write_text(
                (_h.stdout or "") + "\n--- stderr ---\n" + (_h.stderr or ""), encoding="utf-8")
        except Exception as _e:
            res["claude_help_full_error"] = str(_e)
        claude_exec = _run([cl, "-p"], input_text=PROMPT, timeout=180)
        res["claude"] = {"path": cl, "version": _run([cl, "--version"], timeout=30),
                         "exec": claude_exec}
        claude_probe_ok, claude_probe_status = _probe_status(claude_exec)
        res["claude"]["probe_ok"] = claude_probe_ok
        res["claude"]["probe_status"] = claude_probe_status
    # EVO-20261015-1: 把两个通道的探测结果写进共用 health state
    try:
        res["health"] = {
            "codex":  _emit_probe_outcome("codex",  codex_probe_ok,  codex_probe_status),
            "claude": _emit_probe_outcome("claude", claude_probe_ok, claude_probe_status),
            "state":  claude_gate._load_health_state(),
        }
    except Exception as _e:
        res["health_error"] = str(_e)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
