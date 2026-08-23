"""一次性快照：当日 regime 已经在 regime_state.json，这里跑信号+决策+事件日历。"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

LOCK_PATH = Path(__file__).with_name(".snapshot_today.lock")
TIMEOUT_SEC = int(os.environ.get("SNAPSHOT_TIMEOUT_SEC", "600"))
AI_ENABLED = os.environ.get("SNAPSHOT_WITH_AI", "1") != "0"
AI_TIMEOUT_SEC = int(os.environ.get("SNAPSHOT_AI_TIMEOUT_SEC", "300"))
SNAPSHOT_LINES: list[str] = []


def p(*a, **kw):
    sep = kw.get("sep", " ")
    end = kw.get("end", "\n")
    text = sep.join(str(x) for x in a)
    if end == "\n":
        SNAPSHOT_LINES.append(text)
    else:
        SNAPSHOT_LINES.append(text + end)
    print(*a, **kw, flush=True)


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_INFORMATION = 0x0400
            h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, 0, pid)
            if not h:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            kernel32.CloseHandle(h)
            return exit_code.value == 259
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _check_lock_or_exit() -> None:
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            p(f"[snapshot] 已有快照进程在跑 (PID={old_pid})，本次退出。")
            p(f"[snapshot] 如确认它已死，删除 {LOCK_PATH.name} 后重试。")
            sys.exit(2)
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_release_lock)


def _release_lock() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def _start_watchdog() -> threading.Timer:
    def _timeout() -> None:
        p(f"[snapshot] 超过 {TIMEOUT_SEC}s 未结束，强制退出。")
        os._exit(124)

    timer = threading.Timer(TIMEOUT_SEC, _timeout)
    timer.daemon = True
    timer.start()
    return timer


def _snapshot_prompt(snapshot_text: str) -> str:
    return f"""请基于下面这份 Trading Agents 快照，输出一份简洁但可执行的 AI 分析。

要求：
1. 先给总判断：今天偏进攻、观望、还是防守。
2. 分 TQQQ / SOXL / DRAM / MULL / GLD 写执行建议，明确“不追高/等回踩/减仓/观望”等动作。
3. 单独指出信号冲突、过期 regime、事件风险、15min 与日K不一致等耦合风险。
4. 不要编造快照里没有的数据；缺数据就说缺数据。
5. 输出中文，直接给结论，不写客套话。

=== 快照开始 ===
{snapshot_text}
=== 快照结束 ===
"""


def _run_ai_analysis() -> None:
    snapshot_text = "\n".join(SNAPSHOT_LINES).strip()
    if not snapshot_text:
        p("[snapshot-ai] 无快照内容，跳过 AI 分析。")
        return

    from config import SIGNALS_DIR

    out_dir = Path(SIGNALS_DIR)
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = out_dir / f"snapshot_{stamp}.txt"
    snapshot_path.write_text(snapshot_text + "\n", encoding="utf-8")

    if not AI_ENABLED:
        p(f"[snapshot-ai] 已关闭；快照保存: {snapshot_path}")
        return

    p("")
    p("=" * 72)
    p("  AI 快照分析")
    p("=" * 72)
    p(f"[snapshot-ai] 快照保存: {snapshot_path}")
    p("[snapshot-ai] 按统一 CLI 策略调用（默认 Codex，不自动消耗 Claude 额度）...")

    try:
        from ai_prompt import (
            print_analysis,
            query_ai_cli,
        )
    except Exception as exc:
        p(f"[snapshot-ai] AI 模块不可用: {exc}")
        return

    prompt = _snapshot_prompt(snapshot_text)
    prompt_path = out_dir / f"ai_prompt_snapshot_{stamp}.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    output, status, provider, fallback_reason = query_ai_cli(
        prompt, timeout=AI_TIMEOUT_SEC
    )

    if not output:
        p(f"[snapshot-ai] 调用失败: {status}")
        p(f"[snapshot-ai] 提问稿: {prompt_path}")
        return

    analysis_path = out_dir / f"ai_analysis_snapshot_{stamp}.md"
    analysis_path.write_text(
        f"# {provider} 快照分析 — {stamp}\n\n{output}\n",
        encoding="utf-8",
    )
    if fallback_reason:
        p(f"[snapshot-ai] 主 CLI 不可用，已按显式策略切换: {fallback_reason}")
    p("")
    p(f"{provider} 快照分析:")
    p("-" * 72)
    try:
        print_analysis(output, provider=provider)
    except TypeError:
        print(output)
    p("-" * 72)
    p(f"[snapshot-ai] 分析保存: {analysis_path}")


def main() -> int:
    _check_lock_or_exit()
    watchdog = _start_watchdog()
    try:
        from config import TICKERS
        from events_watch import generate_event_calendar_report
        from gold_macro import get_gold_macro_signal, format_gold_macro_banner
        from option_walls import format_walls_report
        from strategy_engine import generate_today_signals
        from strategy_runner import SignalStrategy, SCOPE_TRACKED
        from trump_signal import get_trump_signal_cached, format_trump_banner

        GOLD = "US.GLD"
        # 统一 signal 入口（借鉴 FreqTrade IStrategy）—— 一次 emit_all 覆盖
        # TICKERS + TRACKED_TICKERS (含 CBRS/USO/XLV/NVDA 等)，避免每次加新
        # ticker 忘了 emit 导致 dashboard 卡片缺
        strategy = SignalStrategy(gold_ticker=GOLD)
        p("=" * 72)
        p("  今日信号快照 (after-hours)")
        p("=" * 72)

        # Trump signal banner（接到 events_signal 之前先单独展示）
        try:
            ts = get_trump_signal_cached(hours=24)
            for line in format_trump_banner(ts):
                p(line)
        except Exception as e:
            p(f"trump_signal ERR: {e}")
        p("")

        # Gold macro signal banner（real rate / DXY / WALCL / FOMC）
        try:
            gm = get_gold_macro_signal()
            for line in format_gold_macro_banner(gm):
                p(line)
        except Exception as e:
            p(f"gold_macro ERR: {e}")
        p("")

        # 期权风险 banner（三巫日 + GEX + Call/Put Wall + Max Pain）
        # MULL 加入 → 通过 MU 代理算 wall（UNDERLYING_WALL_PROXIES）
        try:
            for line in format_walls_report(["TQQQ", "SOXL", "DRAM", "MULL", "GLD", "SPY", "QQQ"]):
                p(line)
        except Exception as e:
            p(f"option_walls ERR: {e}")
        p("")

        # JP Social Reco banner（博主推荐 + 星标 + 看好逻辑 + 历史胜率）
        try:
            from jp_social_reco import (get_jp_social_with_backtest,
                                          format_jp_social_banner_enhanced)
            jp_sig = get_jp_social_with_backtest(
                lookback_hours=168, use_llm=True, verify_opend=False,
                attach_backtest=True)
            for line in format_jp_social_banner_enhanced(jp_sig):
                p(line)
        except Exception as e:
            p(f"jp_social ERR: {e}")
        p("")

        # 全 universe (TICKERS + TRACKED_TICKERS + GLD) 一次 emit：
        #   - 各 ticker _latest.json 都会写（fix: 之前 TRACKED 不 emit）
        #   - events 在 SignalStrategy 内部缓存复用，不会 per-ticker 重拉
        #   - 单 ticker 失败不影响其它（内部 try/except）
        market_signals: dict = {}
        signal_results = strategy.emit_all(scope=SCOPE_TRACKED)
        # GLD 单独处理（scope=tracked 不包含 GLD，与 orchestrator 的 _gold_cycle 分离一致）
        signal_results[GOLD] = strategy.emit_signal(GOLD)

        for tk, res in signal_results.items():
            p(f"\n--- {tk} ---")
            if "error" in res:
                p(f"  ERR: {res['error']}")
                continue
            m = res.get("market") or {}
            dec = res.get("decision") or {}
            market_signals[tk] = m
            p(
                f"  action = {dec.get('action', '?')}   "
                f"conf = {dec.get('confidence', '?')}   "
                f"regime = {dec.get('regime', '?')}   "
                f"engine = {dec.get('engine', '?')}"
            )
            close = m.get("close") if isinstance(m, dict) else None
            chg = m.get("pct_chg") if isinstance(m, dict) else None
            if close is not None:
                p(f"  close={close}  pct_chg={chg}")
            for r in dec.get("reasons", [])[:8]:
                p(f"   - {r}")
            if res.get("_emit_error"):
                p(f"  warn: {res['_emit_error']}")

        p("\n" + "=" * 72)
        p("  今日信号实况 (多指标共振)")
        p("=" * 72)
        try:
            p(generate_today_signals(market_signals))
        except Exception as e:
            p(f"today signals ERR: {e}")
            traceback.print_exc()

        p("\n" + "=" * 72)
        p("  事件日历")
        p("=" * 72)
        try:
            p(generate_event_calendar_report())
        except Exception as e:
            p(f"event calendar ERR: {e}")
            traceback.print_exc()

        _run_ai_analysis()
        p("\n[done]")
        return 0
    finally:
        watchdog.cancel()
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
