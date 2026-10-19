"""系统自我进化 (2026-10-12 用户要求: 给系统更多自主能动性, 但每次开发功能前先审批).

流程: 提案 (只读) → 用户批准 → 隔离副本里开发 → 测试 → 上线
  1. propose():  每天收盘后, 本机 Claude Code 读复盘总结 / 日志报错 / 数据质量 / 主线读数,
                 对照彭博 (Bloomberg) 机构级量化与投研系统找差距, 提出 ≤3 条改进提案. 不改代码.
  2. 审批:       本机看板按钮或聊天 (approve / reject). 没批准的提案什么都不会发生.
  3. develop():  在 .evo_worktrees/<id> (分支 evo/<id>) 里让 Claude Code 只用读写文件工具改代码;
                 先跑一遍基线测试, 改完再跑, 不允许出现新的失败 (最多给一次修复机会).
  4. 上线:       改动不碰禁区 + 测试通过 → 只动看板/报告/研究脚本的自动合并;
                 碰到下单/决策/交易进程相关文件 → 状态 ready_for_review, 用户在看板点"上线"后才合并.
护栏 (提案和开发都改不了, 合并前强制检查):
  · 禁止改动的文件: 本模块、看门狗白名单、密钥、kill switch 等 (FORBIDDEN_FILES)
  · 禁止出现在改动里的内容: 账户模式、现金纪律、组合风控门、止损开关 (FORBIDDEN_PATTERNS)
  · 总开关: agents/.evolution_off 存在 → 全部暂停
  · 限流: 同时只开发 1 条; 每周最多 WEEKLY_PROPOSAL_CAP 条提案
  · 留痕: signals/evolution/log.jsonl; 提交信息以 [evo] 开头, 可随时 git revert
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
EVO_DIR = HERE / "signals" / "evolution"
PROPOSALS = EVO_DIR / "proposals.json"
LOG = EVO_DIR / "log.jsonl"
TOKEN = EVO_DIR / ".token"
KILL_SWITCH = HERE / ".evolution_off"
WORKTREES = REPO / ".evo_worktrees"
PY = r"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"

MAX_PER_RUN = 3
WEEKLY_PROPOSAL_CAP = 7
DEV_TIMEOUT_SEC = 40 * 60
TEST_TIMEOUT_SEC = 20 * 60

# 改动若涉及这些文件 → 直接拒绝合并 (防止"自我进化"给自己放权)
FORBIDDEN_FILES = (
    "agents/evolution.py", "agents/_evolution_propose.bat", "agents/_evolution_worker.bat",
    "agents/_webui_watchdog.py", "agents/_watchdog.py", "agents/.evolution_off",
    "agents/tests/test_evolution.py", "agents/secrets.local.json", ".gitignore",
    "agents/snap_public.bat", "agents/_publish_gh_pages.py",
)
FORBIDDEN_PREFIXES = (".git", ".github/", "agents/.sec", "docs/")
# 改动行 (增或删) 里出现这些 → 拒绝合并: 账户模式 / 现金纪律 / 组合风控门 / 止损开关
FORBIDDEN_PATTERNS = (
    r"TRADING_ACCOUNT_MODE", r"TrdEnv\.REAL", r"\bREAL\b.*account", r"CASH_FLOOR_USD",
    r"BUY_FUNDING_SOURCES", r"RESTORE_FUNDING_SOURCES", r"def _fund_buy_qty", r"def restore_cash_floor",
    r"pretrade_portfolio_gate", r"monitor_software_stops", r"TRAILING_STOP_BASE_PCT",
    r"TRADER_DRY_RUN", r"MOOMOO_ACC_ID", r"FRED_API_KEY", r"api_key",
)
# 改到这些文件 → 测试通过后仍需用户点"上线"
TRADING_CRITICAL = (
    "agents/paper_trader.py", "agents/decision_agent.py", "agents/orchestrator.py",
    "agents/claude_gate.py", "agents/trading_contracts.py", "agents/auto_rebalance.py",
    "agents/thesis_config.py", "agents/signals/thesis_config.json", "agents/config.py",
    "agents/portfolio_analytics.py", "agents/option_flow.py", "agents/ai_prompt.py",
    "agents/decision_context.py", "agents/semi_risk_guard.py", "agents/supply_calendar.py",
    "agents/fill_ledger.py", "agents/cohort_tracker.py",
)
STATUSES = ("proposed", "approved", "rejected", "developing", "dev_failed", "blocked",
            "ready_for_review", "merge_approved", "merged", "merge_failed", "merge_rejected")


# ── 存储 ─────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(event: str, **kw) -> None:
    EVO_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), "event": event, **kw}, ensure_ascii=False) + "\n")


def load() -> list[dict]:
    try:
        return json.loads(PROPOSALS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save(items: list[dict]) -> None:
    EVO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROPOSALS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    # 2026-10-19: Windows 上看板/杀毒软件短暂占用目标文件会让 os.replace 报 WinError 5 (EVO-20261014-3 误判失败)
    for attempt in range(6):
        try:
            os.replace(tmp, PROPOSALS)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.25 * (attempt + 1))


def get(items: list[dict], pid: str) -> dict | None:
    return next((p for p in items if p.get("id") == pid), None)


def _set_status(p: dict, status: str, note: str = "", by: str = "system") -> None:
    assert status in STATUSES, status
    p["status"] = status
    p.setdefault("history", []).append({"ts": _now(), "status": status, "by": by, "note": note})
    _log("status", id=p["id"], status=status, by=by, note=note)


def enabled() -> bool:
    return not KILL_SWITCH.exists()


def token() -> str:
    """本机看板操作用的随机口令 (不进公开快照)."""
    EVO_DIR.mkdir(parents=True, exist_ok=True)
    if not TOKEN.exists():
        TOKEN.write_text(secrets.token_hex(16), encoding="utf-8")
    return TOKEN.read_text(encoding="utf-8").strip()


def _request_worker() -> None:
    (HERE / "signals" / "job_request_evo_worker.json").write_text(
        json.dumps({"reason": "evolution approval", "ts": _now()}), encoding="utf-8")


# ── 审批 (看板按钮 / 聊天) ──────────────────────────────────────────────────
ACTIONS = {
    # action: (允许的当前状态, 新状态, 是否唤起 worker)
    "approve": (("proposed",), "approved", True),
    "reject": (("proposed",), "rejected", False),
    "approve_merge": (("ready_for_review",), "merge_approved", True),
    "reject_merge": (("ready_for_review",), "merge_rejected", False),
    # 2026-10-13: 失败后由用户 (或额度恢复后) 重新开发; 仍然不会自动上线交易代码
    "retry": (("dev_failed", "blocked", "merge_failed"), "approved", True),
}

# Claude 额度/限流导致的失败不是代码问题: 退回 approved, 由 evo_worker 定时任务稍后重试
QUOTA_MARKERS = ("hit your limit", "usage limit", "rate limit", "rate_limit", "quota", "overloaded", " 429")


def _quota_hit(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in QUOTA_MARKERS)


def act(pid: str, action: str, by: str = "user", note: str = "") -> dict:
    if action not in ACTIONS:
        return {"ok": False, "error": f"未知操作 {action}"}
    items = load()
    p = get(items, pid)
    if not p:
        return {"ok": False, "error": f"没有提案 {pid}"}
    allowed, new, wake = ACTIONS[action]
    if p.get("status") not in allowed:
        return {"ok": False, "error": f"{pid} 当前状态 {p.get('status')} 不能 {action}"}
    _set_status(p, new, note=note, by=by)
    save(items)
    if wake:
        _request_worker()
    return {"ok": True, "id": pid, "status": new}


# ── 提案 ─────────────────────────────────────────────────────────────────────
BLOOMBERG_REF = """对照参考 (彭博 Bloomberg 机构级量化与投研体系的常见模块, 用来找差距, 不要求照搬):
- PORT: 组合风险、因子暴露、业绩归因 (收益来自哪些因子/板块/个股/择时)
- MARS / 风险模型: 情景压力测试、VaR、相关性与集中度
- BQuant: 可复现的研究与回测环境, 研究→上线的流程管理
- EQS: 多条件选股与筛选回测
- BTCA: 交易成本分析 (滑点、冲击成本、成交质量)
- ECO / 事件日历 与 新闻情绪: 宏观事件、个股事件、新闻情绪对价格的影响
- 告警与工作流: 阈值告警、复盘归档、决策留痕"""


def _recent_errors(days: int = 2, limit: int = 40) -> list[str]:
    out, seen = [], set()
    pat = re.compile(r"ERROR|Traceback|FAIL|Exception|失败", re.I)
    for i in range(days):
        d = (datetime.now(timezone(timedelta(hours=9))) - timedelta(days=i)).strftime("%Y%m%d")
        f = HERE / "logs" / f"run_{d}.log"
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if pat.search(line):
                    body = re.sub(r"^\S+ \S+ ", "", line)[:200]
                    key = re.sub(r"\d+", "#", body)
                    if key not in seen:
                        seen.add(key)
                        out.append(line[:240])
        except OSError:
            continue
    return out[-limit:]


def postmortem_summary(limit: int = 30) -> dict:
    """卖出复盘归档的汇总: 性质分布、事后结论分布、反复出现的局限性."""
    arch = HERE / "signals" / "postmortem" / "archive.jsonl"
    rows = []
    try:
        for line in arch.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    natures, verdicts, lims = {}, {}, []
    for r in rows:
        natures[r.get("nature", "?")] = natures.get(r.get("nature", "?"), 0) + 1
        a = r.get("analysis") or {}
        v = (a.get("hindsight") or {}).get("verdict")
        if v:
            verdicts[v] = verdicts.get(v, 0) + 1
        for l in a.get("limitations") or []:
            lims.append(f"{r.get('ticker')}: {l.get('limitation')}")
    return {"n": len(rows), "nature": natures, "hindsight_verdict": verdicts, "limitations": lims[-25:]}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_propose_prompt(items: list[dict]) -> str:
    recent = [{"id": p["id"], "title": p.get("title"), "status": p.get("status"),
               "reject_note": next((h.get("note") for h in reversed(p.get("history", []))
                                    if h.get("status") in ("rejected", "merge_rejected")), None)}
              for p in items[-30:]]
    theme = _read_json(HERE / "signals" / "usd_theme.json")
    dq = _read_json(HERE / "signals" / "data_quality_report.json")
    files = sorted(p.name for p in HERE.glob("*.py"))
    ctx = {
        "卖出复盘总结": postmortem_summary(),
        "最近两天日志里的报错": _recent_errors(),
        "数据质量": {k: dq.get(k) for k in ("status", "critical_files", "warning_files", "checked_files")},
        "交易主线": {"strength": theme.get("strength"), "metrics": theme.get("metrics"),
                   "invalidation_triggered": theme.get("invalidation_triggered")},
        "已有提案 (避免重复; 被拒的看拒绝理由)": recent,
        "agents 目录下的模块": files,
    }
    return f"""你是这个美股模拟盘量化系统的研发负责人。请根据下面的材料提出改进提案。
你现在只能提案, 不能改代码; 每条提案要等系统所有者批准后才会开发。

原则 (必须遵守):
1. 先根据卖出复盘做总结 (反复出现的问题是什么), 提案要从这些问题或报错出发, 写清证据.
2. 涉及交易规则 (买卖、仓位、止损、筛选) 的提案, 必须先讲逻辑, 并给出预注册检验方案
   (检验标准事先写死, 训练期 / 样本外, 两期方向一致才算通过); 检验不过就不上线.
3. 不得提议: 拉黑标的或"以后别碰"(亏损本身不是理由); 借保证金; 改账户模式; 关闭或放松现金纪律、
   组合风控门、止损; 修改自我进化模块或看门狗白名单.
4. 优先小步、可验证、可回滚的改动. 没有值得做的就返回空列表, 不要凑数.
5. 用中文.

{BLOOMBERG_REF}

材料:
{json.dumps(ctx, ensure_ascii=False, indent=1)}

只输出 JSON, 不要其他文字, 最多 {MAX_PER_RUN} 条:
{{"review_summary": "卖出复盘总结 (3-5 句)",
 "proposals": [{{"title": "...", "category": "trading|research|dashboard|infra",
   "problem": "...", "evidence": ["..."], "logic": "...", "bloomberg_ref": "对应彭博哪个模块/做法, 没有就写无",
   "change": {{"files": ["agents/xxx.py"], "description": "..."}},
   "test_plan": "...", "risk": "...", "effort": "S|M|L"}}]}}
"""


def parse_proposals(text: str | None) -> tuple[str, list[dict]]:
    if not text:
        return "", []
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return "", []
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "", []
    out = []
    for p in (d.get("proposals") or [])[:MAX_PER_RUN]:
        if not isinstance(p, dict) or not p.get("title") or not p.get("problem"):
            continue
        files = [str(f) for f in ((p.get("change") or {}).get("files") or [])]
        if any(_is_forbidden_file(f) for f in files):
            continue                      # 提议改禁区文件 → 直接丢弃
        p["category"] = p.get("category") if p.get("category") in ("trading", "research", "dashboard", "infra") else "infra"
        out.append(p)
    return str(d.get("review_summary") or ""), out


def _week_count(items: list[dict]) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    return sum(1 for p in items if datetime.fromisoformat(p["created_at"]) >= since)


def propose(ai=None) -> dict:
    if not enabled():
        return {"skipped": "kill switch"}
    items = load()
    room = min(MAX_PER_RUN, WEEKLY_PROPOSAL_CAP - _week_count(items))
    if room <= 0:
        return {"skipped": "weekly cap"}
    if ai is None:
        from ai_prompt import query_ai_cli
        ai = lambda prompt: query_ai_cli(prompt, timeout=600, complexity="complex")
    out, status, provider, _ = ai(build_propose_prompt(items))
    summary, new = parse_proposals(out)
    titles = {p.get("title") for p in items}
    day = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    n0 = sum(1 for p in items if p["id"].startswith(f"EVO-{day}"))
    added = []
    for p in new[:room]:
        if p["title"] in titles:
            continue
        p["id"] = f"EVO-{day}-{n0 + len(added) + 1}"
        p["created_at"] = _now()
        p["provider"] = provider
        p["status"] = "proposed"
        p["history"] = [{"ts": p["created_at"], "status": "proposed", "by": provider or "ai", "note": ""}]
        items.append(p)
        added.append(p["id"])
    save(items)
    (EVO_DIR / "last_review_summary.json").write_text(
        json.dumps({"ts": _now(), "summary": summary, "provider": provider, "status": status},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    _log("propose", added=added, provider=provider, status=str(status)[:120])
    return {"added": added, "summary": summary}


# ── 护栏检查 ─────────────────────────────────────────────────────────────────
def _is_forbidden_file(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("./")
    return p in FORBIDDEN_FILES or any(p.startswith(x) for x in FORBIDDEN_PREFIXES)


def guard_diff(changed_files: list[str], diff_text: str) -> list[str]:
    """返回违规原因列表 (空 = 通过)."""
    why = [f"禁止修改的文件: {f}" for f in changed_files if _is_forbidden_file(f)]
    for line in diff_text.splitlines():
        if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
            continue
        for pat in FORBIDDEN_PATTERNS:
            if re.search(pat, line):
                why.append(f"改动涉及受保护内容 /{pat}/: {line[:120]}")
                break
    return why


def needs_review(changed_files: list[str]) -> bool:
    return any(f.replace("\\", "/") in TRADING_CRITICAL for f in changed_files)


# ── 开发 / 测试 / 合并 (Windows 上由 evo_worker 任务执行) ───────────────────
def _git(args, cwd=None, timeout=120) -> subprocess.CompletedProcess:
    # cwd 默认在调用时取 REPO (不能写成默认参数, 否则测试里 patch REPO 无效, 会碰到真实仓库)
    return subprocess.run(["git", *args], cwd=str(cwd or REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def run_tests(agents_dir: Path) -> dict:
    """unittest discover; 返回 {failed: set(test ids), ran, ok}."""
    py = PY if Path(PY).exists() else sys.executable
    r = subprocess.run([py, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=str(agents_dir), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=TEST_TIMEOUT_SEC,
                       env=dict(os.environ, PYTHONUTF8="1", TRADER_DRY_RUN="1"))
    text = (r.stdout or "") + (r.stderr or "")
    failed = set(re.findall(r"^(?:FAIL|ERROR): (\S+ \([^)]+\))", text, re.M))
    ran = re.search(r"Ran (\d+) tests?", text)
    return {"failed": sorted(failed), "ran": int(ran.group(1)) if ran else 0,
            "ok": r.returncode == 0, "tail": text[-3000:], "text": text}


def failure_details(text: str, ids, limit: int = 8000) -> str:
    """2026-10-18: 只摘出新增失败测试的 traceback 块 (以前只给最后 3000 字符, 常常全是无关输出,
    修复轮的 Claude 看不到真正的报错, 两次开发都卡在同一个导入错误上)."""
    if not text:
        return ""
    sep = "=" * 70
    blocks = [b for b in text.split(sep) if b.strip()]
    want = [b.strip() for b in blocks
            if b.lstrip().startswith(("FAIL:", "ERROR:")) and any(i in b[:500] for i in ids)]
    return ("\n\n".join(want) or text[-3000:])[:limit]


def _save_test_log(pid: str, label: str, res: dict) -> None:
    try:
        d = HERE / "logs" / "evo_tests"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pid}_{label}.txt").write_text(res.get("text") or res.get("tail") or "", encoding="utf-8")
    except Exception:
        pass


# 2026-10-19 用户决定: 框架/交易相关仍用最高档 Opus; 只有不碰交易代码的小改动 (infra/research, effort S) 用 Sonnet.
DEV_MODEL_HIGH = os.environ.get("EVO_DEV_MODEL_HIGH", "opus")
DEV_MODEL_LIGHT = os.environ.get("EVO_DEV_MODEL_LIGHT", "sonnet")


def dev_model(p: dict) -> str:
    files = [str(f).replace("\\", "/") for f in (p.get("change") or {}).get("files") or []]
    touches_trading = any(f in TRADING_CRITICAL for f in files)
    if p.get("category") == "trading" or touches_trading or str(p.get("effort", "M")).upper() != "S":
        return DEV_MODEL_HIGH
    return DEV_MODEL_LIGHT


def _claude_dev(worktree: Path, prompt: str, model: str | None = None) -> tuple[bool, str]:
    from ai_prompt import _find_claude_cli, _hidden_cli_subprocess_kwargs
    cli = _find_claude_cli()
    if not cli:
        return False, "claude CLI 未安装"
    cmd = [cli, "-p", "--permission-mode", "acceptEdits",
           "--allowedTools", "Read,Edit,Write,Glob,Grep",
           # 显式禁止命令执行/联网: 开发只能读写 worktree 里的文件, 测试由 evolution.py 自己跑
           "--disallowedTools", "Bash,WebFetch,WebSearch,NotebookEdit",
           "--no-session-persistence", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    # 参数已核对 claude 2.1.138 --help (logs/claude_help_full.txt, 2026-10-12)
    try:
        r = subprocess.run(cmd, input=prompt, cwd=str(worktree), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=DEV_TIMEOUT_SEC,
                           **_hidden_cli_subprocess_kwargs())
        return r.returncode == 0, (r.stdout or r.stderr or "")[-4000:]
    except subprocess.TimeoutExpired:
        return False, "timeout"


def build_dev_prompt(p: dict, test_failures: str = "") -> str:
    base = f"""你在一个美股模拟盘量化系统的仓库副本里工作 (当前目录就是仓库根目录, 代码在 agents/).
请实现下面这条已获批准的提案。只用读写文件, 不要运行命令。

提案 {p['id']}: {p.get('title')}
问题: {p.get('problem')}
逻辑: {p.get('logic')}
改动: {json.dumps(p.get('change'), ensure_ascii=False)}
检验方案: {p.get('test_plan')}

要求:
- 只改实现这条提案所需的文件; 为新行为补 unittest 测试 (放在 agents/tests/, 风格参考现有测试).
- 涉及交易规则的, 只实现提案里写的检验/影子记录, 不得直接改变下单行为, 除非提案明确要求且附检验方案.
- 不得修改: {', '.join(FORBIDDEN_FILES)}; 不得改动账户模式、现金纪律、组合风控门、止损开关、密钥.
- 注释和说明用中文, 沿用仓库现有风格.
- 最后用 3-5 行中文总结你改了什么.
"""
    if test_failures:
        base += f"\n上一次改动后有测试失败, 请修复 (不要删除或跳过测试):\n{test_failures[-2500:]}\n"
    return base


def develop(pid: str, dev=None, tests=None) -> dict:
    """对一条 approved 提案: 建隔离分支 → 基线测试 → Claude 开发 → 测试 → 护栏 → 合并或待审."""
    tests = tests or run_tests
    items = load()
    p = get(items, pid)
    if not p or p.get("status") != "approved":
        return {"ok": False, "error": "not approved"}
    if dev is None:
        _model = dev_model(p)
        _log("dev_model", id=pid, model=_model)

        def dev(wt_, prompt_):
            fn = _claude_dev                      # 运行时取 (测试可能替换成只收两个参数的函数)
            import inspect
            try:
                takes_model = "model" in inspect.signature(fn).parameters
            except (TypeError, ValueError):
                takes_model = False
            return fn(wt_, prompt_, model=_model) if takes_model else fn(wt_, prompt_)
    _set_status(p, "developing"); save(items)
    branch, wt = f"evo/{pid}", WORKTREES / pid
    WORKTREES.mkdir(parents=True, exist_ok=True)
    # 2026-10-18: 重试时如果分支上已有上次的 [evo-wip] 改动, 在它基础上续做 (只跑修复轮), 省 Claude 额度
    head_msg = _git(["log", "-1", "--format=%s", branch]).stdout.strip()
    resumed = head_msg.startswith(f"[evo-wip] {pid}")
    if resumed:
        wt_base = WORKTREES / f"{pid}-base"
        rb = _git(["worktree", "add", "--detach", str(wt_base), "main"])
        if rb.returncode != 0:
            return _finish(pid, "dev_failed", f"worktree(base) 失败: {rb.stderr[:300]}")
        base = tests(wt_base / "agents")
        _git(["worktree", "remove", "--force", str(wt_base)])
        r = _git(["worktree", "add", str(wt), branch])
        if r.returncode == 0:
            # 先把最新 main 合进来 (例如 2026-10-18 补提交的 portfolio_analytics 等模块), 冲突则放弃续做
            m = _git(["merge", "--no-edit", "-q", "main"], cwd=wt)
            if m.returncode != 0:
                _git(["merge", "--abort"], cwd=wt)
                _git(["worktree", "remove", "--force", str(wt)])
                resumed = False
                r = _git(["worktree", "add", "-B", branch, str(wt), "main"])
    if not resumed and not (wt / ".git").exists():
        r = _git(["worktree", "add", "-B", branch, str(wt), "main"])
    if r.returncode != 0:
        return _finish(pid, "dev_failed", f"worktree 失败: {r.stderr[:300]}")
    if not resumed:
        base = tests(wt / "agents")
        _save_test_log(pid, "base", base)
        ok, out = dev(wt, build_dev_prompt(p))
        if not ok and _quota_hit(out):
            return _finish(pid, "approved", f"Claude 额度用完, 稍后自动重试: {out[-160:]}", cleanup=wt)
        if not ok:
            return _finish(pid, "dev_failed", f"开发失败: {out[-300:]}", cleanup=wt)
    else:
        ok, out = True, f"(续做: 基于分支 {branch} 上次的改动)"
    after = tests(wt / "agents")
    _save_test_log(pid, "after", after)
    new_fail = sorted(set(after["failed"]) - set(base["failed"]))
    first_tail = after.get("tail", "")
    if new_fail:                                  # 给一次修复机会 (附上真正的 traceback)
        ok2, out2 = dev(wt, build_dev_prompt(p, failure_details(after.get("text", ""), new_fail)))
        if not ok2 and _quota_hit(out2):
            _keep_wip(pid, wt, "fix round hit quota")
            return _finish(pid, "approved", f"修复轮 Claude 额度用完, 稍后自动重试: {out2[-160:]}", cleanup=wt)
        out = out + "\n--- 修复轮 ---\n" + (out2 or "")
        after = tests(wt / "agents")
        _save_test_log(pid, "after_fix", after)
        new_fail = sorted(set(after["failed"]) - set(base["failed"]))
    # 测试运行会改写被跟踪的运行时数据 (agents/signals/*.json), 不属于开发改动
    _drop_runtime_files(wt)
    _git(["add", "-A"], cwd=wt)
    # 和 main 比 (续做时分支上已有 [evo-wip] 提交, 也要一起审)
    changed = [f for f in _git(["diff", "--cached", "--name-only", "main"], cwd=wt).stdout.splitlines() if f]
    diff = _git(["diff", "--cached", "main"], cwd=wt, timeout=60).stdout
    if not changed:
        return _finish(pid, "dev_failed", "没有产生任何改动", cleanup=wt)
    report = {"changed_files": changed, "tests_before": base["ran"], "tests_after": after["ran"],
              "new_failures": new_fail, "dev_summary": out[-1500:],
              "diffstat": _git(["diff", "--cached", "--stat", "main"], cwd=wt).stdout[-2000:],
              "resumed_from_wip": resumed,
              "test_tail": (failure_details(after.get("text", ""), new_fail) if new_fail
                            else (after.get("tail") or first_tail or ""))[-4000:]}
    items = load(); p = get(items, pid); p["dev_report"] = report; save(items)
    if new_fail:
        _keep_wip(pid, wt, f"{len(new_fail)} new test failures")
        return _finish(pid, "dev_failed", f"新增测试失败 {len(new_fail)} 个 (改动保留在分支 evo/{pid}, 未合并)", cleanup=wt)
    violations = guard_diff(changed, diff)
    if violations:
        items = load(); p = get(items, pid); p["dev_report"]["violations"] = violations; save(items)
        _keep_wip(pid, wt, "guard violations")
        return _finish(pid, "blocked", "; ".join(violations[:3]), cleanup=wt)
    msg = (f"[evo] {pid}: {p.get('title')}\n\n{p.get('problem', '')[:400]}\n\n"
           f"Approved by owner; developed by local Claude Code (evolution worker); "
           f"tests {after['ran']} run, no new failures.\n")
    c = _git(["commit", "-q", "--allow-empty", "-m", msg], cwd=wt)   # 续做且修复轮无新改动时也要落一个正式提交
    if c.returncode != 0:
        return _finish(pid, "dev_failed", f"commit 失败: {c.stderr[:200]}", cleanup=wt)
    if needs_review(changed):
        return _finish(pid, "ready_for_review", "改动涉及交易相关文件, 等待用户确认上线")
    return merge(pid)


RUNTIME_DIR = "agents/signals/"
RUNTIME_KEEP = "agents/signals/backtest_verdicts/"   # 预注册 verdict 是开发产物, 必须保留


def _drop_runtime_files(wt: Path) -> None:
    """测试运行会改写/生成 agents/signals 下的运行时数据, 不属于开发改动; 预注册 verdict 除外.
    (不用 git clean: 整个目录未跟踪时它会连 verdict 一起删)"""
    modified = _git(["diff", "--name-only", "--", RUNTIME_DIR], cwd=wt).stdout.splitlines()
    revert = [f for f in modified if f and not f.startswith(RUNTIME_KEEP)]
    if revert:
        _git(["checkout", "--", *revert], cwd=wt)
    untracked = _git(["ls-files", "--others", "--exclude-standard", "--", RUNTIME_DIR], cwd=wt).stdout.splitlines()
    for f in untracked:
        if f and not f.startswith(RUNTIME_KEEP):
            try:
                (Path(wt) / f).unlink()
            except OSError:
                pass


def _keep_wip(pid: str, wt: Path, why: str) -> None:
    """失败的开发也提交到 evo/<id> 分支 (不合并), 便于汇报时查看到底改了什么."""
    _drop_runtime_files(wt)
    _git(["add", "-A"], cwd=wt)
    _git(["commit", "-q", "--allow-empty", "-m", f"[evo-wip] {pid}: NOT MERGED ({why})"], cwd=wt)


def merge(pid: str) -> dict:
    items = load()
    p = get(items, pid)
    if not p or p.get("status") not in ("developing", "merge_approved"):
        return {"ok": False, "error": "not mergeable"}
    branch = f"evo/{pid}"
    r = _git(["merge", "--no-ff", "--no-edit", "-m", f"[evo] merge {pid}: {p.get('title')}", branch])
    if r.returncode != 0:
        _git(["merge", "--abort"])
        return _finish(pid, "merge_failed", r.stderr[:300] or r.stdout[:300])
    return _finish(pid, "merged", _git(["rev-parse", "--short", "HEAD"]).stdout.strip(),
                   cleanup=WORKTREES / pid, delete_branch=branch)


def _finish(pid: str, status: str, note: str, cleanup: Path | None = None, delete_branch: str | None = None) -> dict:
    items = load()
    p = get(items, pid)
    if p:
        _set_status(p, status, note=note)
        save(items)
    if cleanup is not None:
        _git(["worktree", "remove", "--force", str(cleanup)])
    if delete_branch:
        _git(["branch", "-D", delete_branch])
    return {"ok": status in ("merged", "ready_for_review"), "id": pid, "status": status, "note": note}


# 2026-10-14: 开发会大量消耗本机 Claude 额度, 而交易链路 (claude_gate) 也靠同一账号.
# 只在美股收盘后的窗口开发 (UTC 20:00-04:59 = 日本 05:00-13:59), 额度 5 小时滚动恢复,
# 保证下一个交易时段 (UTC 13:30 起) 前额度已恢复. 合并不调用 Claude, 不受此限.
DEV_WINDOW_UTC_HOURS = (20, 21, 22, 23, 0, 1, 2, 3, 4)


def in_dev_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    # 2026-10-18: 周末美股休市 (UTC 周六全天 / 周日全天), 额度在周一开盘前早已恢复, 可随时开发
    if now.weekday() in (5, 6):
        return True
    return now.hour in DEV_WINDOW_UTC_HOURS


def work(now: datetime | None = None) -> list[dict]:
    """evo_worker 任务: 先合并已确认上线的, 再 (仅开发窗口内) 开发 1 条已批准的."""
    if not enabled():
        return [{"skipped": "kill switch"}]
    done = []
    for p in [x for x in load() if x.get("status") == "merge_approved"]:
        done.append(merge(p["id"]))
    if not in_dev_window(now):
        if any(x.get("status") == "approved" for x in load()):
            done.append({"deferred": "outside dev window (UTC 20-03), protects trading Claude quota"})
        return done
    progressed = True
    queue = sorted([x for x in load() if x.get("status") == "approved"],   # 先批准的先开发
                   key=lambda x: ((x.get("history") or [{}])[-1].get("ts", ""), x["id"]))
    for p in queue[:1]:
        done.append(develop(p["id"]))
        progressed = next((x.get("status") for x in load() if x["id"] == p["id"]), "") != "approved"
    # 一次只开发 1 条 (单次任务时长可控); 还有已批准/已确认的 → 再排一次 worker
    # (只在本次确有进展时才重排, 防止卡在同一条上无限循环)
    if progressed and any(x.get("status") in ("approved", "merge_approved") for x in load()):
        _request_worker()
        done.append({"requeued": True})
    return done


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    log = HERE / "logs" / f"evolution_{cmd or 'none'}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        try:
            if cmd == "propose":
                res = propose()
            elif cmd == "work":
                res = work()
            elif cmd in ACTIONS and len(argv) > 2:
                res = act(argv[2], cmd, by="chat", note=" ".join(argv[3:]))
            else:
                res = {"usage": "evolution.py propose|work|approve|reject|approve_merge|reject_merge <id> [note]"}
            fh.write(json.dumps(res, ensure_ascii=False, indent=1))
            print(json.dumps(res, ensure_ascii=False))
            return 0
        except Exception as e:  # noqa: BLE001
            import traceback
            fh.write(traceback.format_exc())
            print(f"FAILED: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
