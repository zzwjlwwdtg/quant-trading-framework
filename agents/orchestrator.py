"""
Orchestrator — 日K交易者模式，每天5个时间窗口。

架构:
  08:30 ET  盘前扫描   — 隔夜风险 + 日K主信号
  09:20 ET  开盘前扫描 — 日K主信号 + 策略回测 + 规则进化
  10:00 ET  开盘后确认 — 开盘价格确认 + 日K主信号
  12:00 ET  午盘跟踪   — 日K主信号，确认方向是否有变
  15:45 ET  收盘前确认 — 日K主信号 + 60分钟K入场辅助
  16:00 ET  收盘总结   — 每日复盘

可执行动作信号时附加60分钟K入场参考。

Run:  python orchestrator.py
Stop: Ctrl-C
"""

import os
import schedule
import time
import sys
import atexit
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import TICKERS, DAILY_WINDOWS_ET, WATCH_ONLY_TICKERS
from market_watch import get_market_signal
from futures_watch import get_futures_signal
from events_watch import get_events_signal, get_gold_events_signal
from decision_agent import get_decision, get_gold_decision
from data_feeds import fetch_vix, fetch_fear_greed
from fred_feeds import fetch_fred
from notifier import emit, logger, _toast
from i18n import t
from paper_trader import execute as trade_execute, refresh_nav_peak
from claude_gate import apply_claude_gate
from trading_contracts import ORDER_ACTIONS

ET = ZoneInfo("America/New_York")

GOLD_TICKER = "US.GLD"


# ── B. 单实例锁 ────────────────────────────────────────────────────────────
_LOCK_PATH = Path(__file__).parent / ".orchestrator.lock"


def _check_lock_or_exit() -> None:
    """启动时检查锁文件：已有活进程在跑就拒绝启动；否则写入自己 PID。"""
    if _LOCK_PATH.exists():
        try:
            old_pid = int(_LOCK_PATH.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            print(f"[orchestrator] 已有进程在跑 (PID={old_pid})，本次启动取消。")
            print(f"  如确认那个进程已死，删除 {_LOCK_PATH} 后重试。")
            sys.exit(2)
        # 锁文件是孤儿（进程不在了），清掉
        try: _LOCK_PATH.unlink()
        except OSError: pass
    _LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_release_lock)


def _pid_alive(pid: int) -> bool:
    """跨平台检查 PID 是否还存在。"""
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
            return exit_code.value == 259   # STILL_ACTIVE
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def _release_lock() -> None:
    try:
        if _LOCK_PATH.exists() and _LOCK_PATH.read_text().strip() == str(os.getpid()):
            _LOCK_PATH.unlink()
    except OSError:
        pass


# ── 源码变更自愈：任一关键文件 mtime 前进 → 退出让 watchdog 拉起新进程 ─────────
# 场景：修 bug commit 后，忘记重启 orchestrator，老 bytecode 常驻内存继续跑坏逻辑
# （2026-08-01 execute() UnboundLocalError 就是这么被隐藏 15 天没触发买单）
_CRITICAL_SOURCE_FILES = (
    "orchestrator.py", "paper_trader.py", "decision_agent.py",
    "trading_contracts.py", "claude_gate.py",
    "config.py",   # WATCH_ONLY_TICKERS / TICKERS 变了要重启 orchestrator
)
_STARTUP_MTIMES: dict[str, float] = {}


def _record_startup_mtimes() -> None:
    base = Path(__file__).parent
    for name in _CRITICAL_SOURCE_FILES:
        try:
            _STARTUP_MTIMES[name] = (base / name).stat().st_mtime
        except OSError:
            continue
    logger.info(
        f"[self-restart] 监控 {len(_STARTUP_MTIMES)} 个源文件的 mtime "
        f"(修改后 ≤30s 自动重启): {', '.join(sorted(_STARTUP_MTIMES.keys()))}"
    )


def _detect_code_change() -> str | None:
    """任一关键源文件 mtime 前进 ≥5s → 返回文件名（提示需要重启）。"""
    base = Path(__file__).parent
    for name, orig_mt in list(_STARTUP_MTIMES.items()):
        try:
            cur_mt = (base / name).stat().st_mtime
        except OSError:
            continue
        if cur_mt > orig_mt + 5.0:
            return name
    return None


def _exit_for_code_change(changed: str) -> None:
    """打 log + 通知 + os._exit(0)。watchdog 下一 cycle 会拉起新进程（自带新代码）。

    用 os._exit() 而非 sys.exit()：moomoo OpenD 连接是非 daemon 线程，
    sys.exit 会被它 hang 住不死；os._exit 无条件杀进程，绕过 atexit
    （lock 由 watchdog 的 stale-PID 检测清理，OK）。
    """
    logger.warning(f"[self-restart] 源文件 {changed} 已更新 → 主动退出以加载新代码")
    try:
        from notifications import send_alert
        send_alert(f"🔄 orchestrator 自愈重启：{changed} 源码更新，watchdog 将拉起新进程",
                   level="info")
    except Exception:
        pass
    # 手动清 lock（os._exit 绕过 atexit）
    try:
        if _LOCK_PATH.exists() and _LOCK_PATH.read_text().strip() == str(os.getpid()):
            _LOCK_PATH.unlink()
    except OSError:
        pass
    os._exit(0)

# 防止同一窗口重复触发
_last_report_session = {"open": None, "close": None}
_last_window_run: dict[str, str | None] = {w[2]: None for w in DAILY_WINDOWS_ET}


def _in_daily_window() -> str | None:
    """
    返回当前尚未完成的日K交易窗口名称；不在窗口内返回 None。

    此函数只检查资格，不提交完成状态。完成状态必须在整个窗口任务成功
    返回后由 _mark_window_complete() 写入，失败时下一次 5 分钟 tick 可重试。
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return None
    t     = now.hour * 60 + now.minute
    today = now.date().isoformat()
    for start, end, name in DAILY_WINDOWS_ET:
        if start <= t <= end and _last_window_run[name] != today:
            return name
    return None


def _mark_window_complete(window: str) -> None:
    """Commit a window only after all of its scheduled work has returned."""
    if window in _last_window_run:
        _last_window_run[window] = datetime.now(ET).date().isoformat()


def _session() -> str:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return "closed(weekend)"
    t = now.hour * 60 + now.minute
    if 240 <= t < 570:   return "pre-market"
    if 570 <= t < 960:   return "open"
    if 960 <= t < 1200:  return "after-hours"
    return "closed"


def _report_trigger() -> str | None:
    """
    返回 'open' 或 'close' 表示应生成报告，否则返回 None。
    窗口: 开盘后 0-14 分钟 (9:30-9:44 ET) / 收盘后 0-14 分钟 (16:00-16:14 ET)。
    每个窗口只触发一次（用日期防重复）。
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return None
    today = now.date().isoformat()
    t = now.hour * 60 + now.minute

    if 570 <= t <= 584:  # 9:30–9:44 ET
        if _last_report_session["open"] != today:
            _last_report_session["open"] = today
            return "open"
    if 960 <= t <= 974:  # 16:00–16:14 ET
        if _last_report_session["close"] != today:
            _last_report_session["close"] = today
            return "close"
    return None


def _run_report(session_type: str) -> None:
    logger.info(f"\n{'='*64}")
    sess_disp = t(f"生成{session_type}报告...",
                  f"{('寄付' if session_type == 'open' else '引け')}レポート生成中...")
    logger.info(sess_disp)
    try:
        from report_generator import generate_report
        label = (t("开盘报告", "寄付レポート") if session_type == "open"
                 else t("收盘总结", "引けまとめ"))
        report, fpath = generate_report(label)
        logger.info(report)
        logger.info(t(f"报告已保存: {fpath}", f"レポート保存先: {fpath}"))
        _toast(t(f"{label}已生成", f"{label}生成完了"),
               t(f"查看终端或 {fpath.split(chr(92))[-1]}",
                 f"ターミナルまたは {fpath.split(chr(92))[-1]} を確認"))
    except Exception as exc:
        logger.error(t(f"报告生成失败: {exc}", f"レポート生成失敗: {exc}"))

    # 开盘时运行策略回测 + 进化器
    if session_type == "open":
        try:
            from strategy_engine import fetch_kline_history
            from strategy_evolver import run_evolver
            for tkr in TICKERS + [GOLD_TICKER]:
                df = fetch_kline_history(tkr)
                if df is not None and len(df) >= 80:
                    survivors, _ = run_evolver(tkr, df, is_gold=(tkr == GOLD_TICKER))
                    logger.info(t(
                        f"  [{tkr.split('.')[-1]}] 规则进化完成: {len(survivors)} 条规则",
                        f"  [{tkr.split('.')[-1]}] ルール進化完了: {len(survivors)} 件",
                    ))
        except Exception as exc:
            logger.error(t(f"规则进化失败: {exc}", f"ルール進化失敗: {exc}"))

        # 技术形态胜率排行（历史回测视图）
        try:
            from strategy_engine import generate_pattern_leaderboard
            logger.info(generate_pattern_leaderboard(TICKERS, GOLD_TICKER))
        except Exception as exc:
            logger.error(f"胜率排行生成失败: {exc}")

        # 今日信号实况（实操视图：当前指标 + 触发规则 + 净信号）
        try:
            from strategy_engine import generate_today_signals
            from market_watch  import get_market_signal
            from futures_watch import get_futures_signal
            market_signals = {}
            for tkr in TICKERS:
                try: market_signals[tkr] = get_market_signal(tkr)
                except Exception as e: logger.error(f"today signals [{tkr}]: {e}")
            try: market_signals[GOLD_TICKER] = get_futures_signal(GOLD_TICKER)
            except Exception as e: logger.error(f"today signals [{GOLD_TICKER}]: {e}")
            logger.info(generate_today_signals(market_signals))
        except Exception as exc:
            logger.error(f"今日信号实况生成失败: {exc}")

        try:
            from events_watch import generate_event_calendar_report
            logger.info(generate_event_calendar_report())
        except Exception as exc:
            logger.error(t(f"事件日历生成失败: {exc}",
                           f"イベント・カレンダー生成失敗: {exc}"))

        # Trump Truth Social 信号（CLI 结构化解析 + 聚合）
        try:
            from trump_signal import get_trump_signal_cached, format_trump_banner
            ts = get_trump_signal_cached(hours=24)
            for line in format_trump_banner(ts):
                logger.info(line)
        except Exception as exc:
            logger.error(t(f"Trump signal 失败: {exc}",
                           f"Trump signal 失敗: {exc}"))

        # 黄金宏观信号（real rate / DXY / WALCL / FOMC）
        try:
            from gold_macro import get_gold_macro_signal, format_gold_macro_banner
            gm = get_gold_macro_signal()
            for line in format_gold_macro_banner(gm):
                logger.info(line)
        except Exception as exc:
            logger.error(t(f"Gold macro 失败: {exc}",
                           f"Gold macro 失敗: {exc}"))

        # 期权墙分析（Call/Put Wall + Max Pain，volume-based）
        try:
            from option_walls import format_walls_report
            logger.info("")
            for line in format_walls_report():
                logger.info(line)
        except Exception as exc:
            logger.error(t(f"期权墙分析失败: {exc}",
                           f"オプション・ウォール分析失敗: {exc}"))

        # JP Social Reco — 博主推荐标的（含星标 / 看好逻辑 / 历史胜率）
        try:
            from jp_social_reco import (get_jp_social_with_backtest,
                                          format_jp_social_banner_enhanced)
            jp_sig = get_jp_social_with_backtest(
                lookback_hours=168, use_llm=True, verify_opend=False,
                attach_backtest=True)
            logger.info("")
            for line in format_jp_social_banner_enhanced(jp_sig):
                logger.info(line)
        except Exception as exc:
            logger.error(t(f"JP Social Reco 失败: {exc}",
                           f"JP Social Reco 失敗: {exc}"))

        # MACD + ADX 汇总（仅观察，不参与决策）
        try:
            from market_watch import get_market_signal
            from paper_trader import get_active_tickers
            actives = get_active_tickers()
            if actives:
                W = 76
                logger.info("")
                logger.info("+" + "=" * (W - 2) + "+")
                logger.info(f"|  MACD + ADX 汇总  |  仅供观察, 不参与决策".ljust(W - 1) + "|")
                logger.info("+" + "=" * (W - 2) + "+")
                logger.info(f"  {'ticker':<10} {'DIF':>7} {'DEA':>7} {'区域':<8} {'信号':<8} {'ADX':>6} {'强度':<10}")
                logger.info(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*8} {'-'*8} {'-'*6} {'-'*10}")
                for tk in actives:
                    try:
                        m = get_market_signal(tk)
                        if not m or m.get("error"):
                            continue
                        zone_zh = {"bull":"零轴上(多)","bear":"零轴下(空)","neutral":"中性"}.get(
                            m.get("macd_zone","neutral"), m.get("macd_zone","-"))
                        sig_zh  = {"golden":"★金叉","death":"★死叉","none":"-"}.get(
                            m.get("macd_signal","none"), m.get("macd_signal","-"))
                        adx_zh  = {"weak":"无趋势","moderate":"弱趋势","strong":"强趋势","extreme":"极强"}.get(
                            m.get("adx_zone","weak"), m.get("adx_zone","-"))
                        logger.info(
                            f"  {tk:<10} {m.get('macd_dif',0):>+6.2f} {m.get('macd_dea',0):>+6.2f} "
                            f"{zone_zh:<8} {sig_zh:<8} {m.get('adx_14',0):>5.1f} {adx_zh:<10}"
                        )
                    except Exception:
                        continue
                logger.info("=" * W)
        except Exception as exc:
            logger.error(t(f"MACD/ADX 汇总失败: {exc}", f"MACD/ADX サマリー失敗: {exc}"))

        # SOX 板块 PCA 分析
        try:
            from pca_sox import format_pca_report
            logger.info("\n" + "█" * 64)
            logger.info(t("█  SOX 板块多因子分析  █",
                          "█  SOX セクター・マルチファクター分析  █"))
            logger.info("█" * 64)
            for line in format_pca_report():
                logger.info(line)
        except Exception as exc:
            logger.error(t(f"SOX PCA 分析失败: {exc}",
                           f"SOX PCA 解析失敗: {exc}"))

        # === AI Brief 汇总：把 Claude 截尾后会漏看的"领先指标"集中重打印 ===
        # 这块紧贴 Claude CLI 之前 → 100% 落在 trimmed 28K 内
        # 包含：双时区时钟 + 夜盘期货 + 15min 短线 alert + Trump 关键信号
        try:
            _emit_ai_brief()
        except Exception as exc:
            logger.error(t(f"AI Brief 失败: {exc}", f"AI Brief 失敗: {exc}"))

        # === 最后：Claude CLI 综合解读（基于上面所有数据"说人话"） ===
        # 必须放在最末——这样 ai_prompt 读 today log 时能看到完整结构化分析
        # 屏幕上也是用户最后看到的，重要总结不会被顶走
        try:
            from ai_prompt import auto_analyze, print_analysis
            from config import get_today_log_path
            logger.info("\n" + "█" * 64)
            logger.info(t("█  Claude 综合解读（说人话）  █",
                          "█  Claude 総合解読（わかりやすく）  █"))
            logger.info("█" * 64)
            logger.info(t("调用本地 Claude CLI 综合上述全部数据生成解读（额度满时切 Codex CLI）...",
                          "ローカル Claude CLI で上記データを総合解読（上限時は Codex CLI）..."))
            r = auto_analyze("morning")
            if r.get("status") == "ok":
                provider = r.get("provider", "Claude")
                if r.get("fallback_reason"):
                    logger.info(t(
                        f"  Claude 额度/限流不可用，已切换到 Codex CLI: {r['fallback_reason']}",
                        f"  Claude の上限/制限で不可。Codex CLI に切替: {r['fallback_reason']}",
                    ))
                header = t(f"{provider} 早盘策略", f"{provider} 寄付戦略")
                logger.info(f"\n{'='*64}\n{header}:\n{'='*64}")
                print_analysis(r["output"], log_path=get_today_log_path(), provider=provider)
                logger.info(f"{'='*64}\n  " +
                            t(f"详见: {r['analysis_path']}",
                              f"詳細: {r['analysis_path']}"))
            elif r.get("status") == "not_installed":
                logger.info(t(f"  Claude CLI 未安装；提问稿: {r.get('prompt_path')}",
                              f"  Claude CLI 未インストール；質問稿: {r.get('prompt_path')}"))
            else:
                logger.info(t(
                    f"  Claude CLI 调用失败 ({r.get('status')})；提问稿: {r.get('prompt_path')}",
                    f"  Claude CLI 呼出失敗 ({r.get('status')})；質問稿: {r.get('prompt_path')}",
                ))
        except Exception as exc:
            logger.error(t(f"Claude 综合解读失败: {exc}",
                           f"Claude 総合解読失敗: {exc}"))

    # 收盘时运行每日复盘
    if session_type == "close":
        logger.info(t("运行每日复盘...", "日次復盤を実行中..."))
        try:
            from daily_review import generate_review_report
            rv_report = generate_review_report(TICKERS + [GOLD_TICKER])
            logger.info(rv_report)
        except Exception as exc:
            logger.error(t(f"每日复盘失败: {exc}", f"日次復盤失敗: {exc}"))

        # === 最后：Claude CLI 综合复盘（基于上述全部数据"说人话"） ===
        try:
            from ai_prompt import auto_analyze, print_analysis
            from config import get_today_log_path
            logger.info("\n" + "█" * 64)
            logger.info(t("█  Claude 综合复盘（说人话）  █",
                          "█  Claude 総合復盤（わかりやすく）  █"))
            logger.info("█" * 64)
            logger.info(t(
                "调用本地 Claude CLI 综合上述全部数据生成复盘（额度满时切 Codex CLI）...",
                "ローカル Claude CLI で上記データを総合復盤（上限時は Codex CLI）...",
            ))
            r = auto_analyze("review")
            if r.get("status") == "ok":
                provider = r.get("provider", "Claude")
                if r.get("fallback_reason"):
                    logger.info(t(
                        f"  Claude 额度/限流不可用，已切换到 Codex CLI: {r['fallback_reason']}",
                        f"  Claude の上限/制限で不可。Codex CLI に切替: {r['fallback_reason']}",
                    ))
                header = t(f"{provider} 复盘总结", f"{provider} 復盤まとめ")
                logger.info(f"\n{'='*64}\n{header}:\n{'='*64}")
                print_analysis(r["output"], log_path=get_today_log_path(), provider=provider)
                logger.info(f"{'='*64}\n  " +
                            t(f"详见: {r['analysis_path']}",
                              f"詳細: {r['analysis_path']}"))
            elif r.get("status") == "not_installed":
                logger.info(t(
                    f"  Claude CLI 未安装；提问稿已生成: {r['prompt_path']}",
                    f"  Claude CLI 未インストール；質問稿生成済: {r['prompt_path']}",
                ))
                logger.info(t(
                    f"  剪贴板状态: {'已复制' if r.get('clip_ok') else '未复制'}",
                    f"  クリップボード: {'コピー済' if r.get('clip_ok') else '未コピー'}",
                ))
            else:
                logger.info(t(
                    f"  Claude CLI 调用失败 ({r.get('status')})；提问稿: {r.get('prompt_path')}",
                    f"  Claude CLI 呼出失敗 ({r.get('status')})；質問稿: {r.get('prompt_path')}",
                ))
        except Exception as exc:
            logger.error(t(f"AI 复盘失败: {exc}",
                           f"AI 復盤失敗: {exc}"))


def _emit_ai_brief() -> None:
    """汇总"领先指标"在 Claude CLI 之前重打印一次，确保 trimmed 28K 一定包含。

    1. 双时区时钟（ET + JST）防 Claude 时区误读
    2. 夜盘期货 NQ/ES/GC 重打印（之前在 run_cycle 早期，会被 PCA 顶走）
    3. 15min 短线 alert / context 汇总（每只 ETF 一行）
    4. Trump signal 强信号复述（large/extreme 的 verbatim 证据）
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    logger.info("\n" + "█" * 64)
    logger.info(t("█  AI Brief（领先指标汇总，给 Claude 看的）  █",
                  "█  AI Brief（先行指標サマリー、Claude 用）  █"))
    logger.info("█" * 64)

    # 1. 双时区时钟
    try:
        et = _dt.now(ET)
        jst = _dt.now(_tz(_td(hours=9)))
        logger.info(t(
            f"  时间: ET {et.strftime('%Y-%m-%d %H:%M')} = JST {jst.strftime('%Y-%m-%d %H:%M')}",
            f"  時刻: ET {et.strftime('%Y-%m-%d %H:%M')} = JST {jst.strftime('%Y-%m-%d %H:%M')}",
        ))
        logger.info(t(
            "  ⚠ log 里所有日期标签都按 ET 时区。Claude 解读时'今天'= ET 日期，不要按 JST 推断",
            "  ⚠ log の日付ラベルは全て ET 時区。Claude の'今日'= ET 日付、JST で推測しない",
        ))
    except Exception:
        pass

    # 2. 夜盘期货 NQ/ES/GC 重打印
    try:
        from nightwatch import format_nightwatch_lines
        logger.info("")
        logger.info(t("  ─── 夜盘期货（领先指标） ───", "  ─── ナイトセッション先物（先行指標） ───"))
        for line in format_nightwatch_lines():
            logger.info(line)
    except Exception as exc:
        logger.info(t(f"  夜盘期货数据不可用: {exc}",
                      f"  ナイトセッション先物データ不可: {exc}"))

    # 3. 15min 短线信号汇总（直接从 market_watch 拉，不依赖 emit log）
    try:
        from market_watch import get_shortterm_context
        logger.info("")
        logger.info(t("  ─── 15min 短线扫描 ───", "  ─── 15分足ショートタームスキャン ───"))
        for tkr in TICKERS + [GOLD_TICKER]:
            try:
                stx = get_shortterm_context(tkr)
                if not stx:
                    continue
                tk_short = tkr.split(".")[-1]
                direction = stx.get("m15_direction", "neutral")
                score = stx.get("m15_score", 0)
                note = stx.get("m15_note", "")
                if direction in ("long", "short") and score >= 2:
                    arrow = "▲" if direction == "long" else "▼"
                    logger.info(f"  {arrow} [{tk_short}] 短线 {direction}(score={score}): {note}")
                elif note and "无信号" not in note and "シグナルなし" not in note:
                    logger.info(f"  · [{tk_short}] {note}")
                else:
                    logger.info(f"  · [{tk_short}] 15min 无短线信号")
            except Exception as e:
                logger.info(f"  · [{tkr}] 15min 扫描失败: {e}")
    except Exception as exc:
        logger.info(t(f"  15min 短线模块不可用: {exc}",
                      f"  15分足モジュール不可: {exc}"))

    # 4. Trump signal 强信号复述（large/extreme 的 verbatim）
    try:
        from trump_signal import get_trump_signal_cached
        sig = get_trump_signal_cached(hours=24)
        strong = [s for s in sig.get("active_signals", [])
                  if s.get("magnitude") in ("large", "extreme")]
        if strong:
            logger.info("")
            logger.info(t("  ─── Trump 强信号复述（≥large） ───",
                          "  ─── Trump 強シグナル再録（≥large） ───"))
            for s in strong[:5]:
                arrow = {"bullish": "▲", "bearish": "▼"}.get(s.get("direction"), "·")
                logger.info(
                    f"  {arrow} {s.get('event_type'):<14} {s.get('magnitude')} "
                    f"conf={s.get('confidence')} {'/'.join(s.get('tickers_affected',[])[:3])}"
                )
                ev = s.get("verbatim_evidence", "")[:120]
                if ev:
                    logger.info(f"     \"{ev}\"")
    except Exception:
        pass

    logger.info("█" * 64)


def _build_macro() -> dict:
    """每周期拉一次宏观快照: VIX + CNN F&G + FRED。"""
    try:
        vix_val = fetch_vix()
    except Exception:
        vix_val = None
    try:
        fg = fetch_fear_greed()
    except Exception:
        fg = {}
    try:
        fred = fetch_fred()
    except Exception:
        fred = {}

    macro = {
        "vix":      vix_val,
        "fg_score": fg.get("score"),
        "t10y2y":   (fred.get("T10Y2Y") or {}).get("value"),
        "t10yie":   (fred.get("T10YIE") or {}).get("value"),
        "fedfunds": (fred.get("FEDFUNDS") or {}).get("value"),
    }
    logger.info(
        f"  [宏观] VIX:{vix_val or 'N/A'}  F&G:{fg.get('score', 'N/A')}"
        f"  T10Y2Y:{macro['t10y2y'] or 'N/A'}"
        f"  FEDFUNDS:{macro['fedfunds'] or 'N/A'}"
    )
    return macro


def _etf_cycle(events_signal: dict, macro: dict, window: str | None = None,
               tickers: list[str] | None = None) -> None:
    from market_watch import get_intraday_context, get_shortterm_context
    from confluence import get_confluence
    try:
        from regime_today import get_today_regime
        board_regime = get_today_regime()
    except Exception:
        board_regime = None
    iter_tickers = tickers if tickers is not None else TICKERS
    for ticker in iter_tickers:
        try:
            mkt      = get_market_signal(ticker)
            confl    = get_confluence(mkt)
            decision = get_decision(mkt, events_signal, macro,
                                    confluence=confl, board_regime=board_regime)
            decision["session"] = _session()
            decision["confluence"] = confl

            # 60分钟K：可操作信号时附加入场参考
            if decision.get("action") in ORDER_ACTIONS:
                ctx = get_intraday_context(ticker)
                if ctx:
                    decision["h1_context"] = ctx["h1_note"]

            # 15分钟K：每个 cycle 都跑，捕捉短线机会
            stx = get_shortterm_context(ticker)
            if stx:
                decision["m15_context"] = stx["m15_note"]
                decision["m15_direction"] = stx["m15_direction"]
                # 日 K = HOLD 但 15min 出现强短线信号 → 升级为短线 WATCH
                if (decision.get("action") == "HOLD"
                    and stx["m15_score"] >= 2
                    and stx["m15_direction"] in ("long", "short")):
                    decision["m15_alert"] = (
                        f"日K中性，但15min触发短线"
                        f"{'做多' if stx['m15_direction'] == 'long' else '做空'}信号"
                    )

            decision = apply_claude_gate(ticker, mkt, events_signal, decision, macro, window)

            emit(mkt, events_signal, decision)

            # 主动推送 crisis regime（dedup 5 min，避免同一 crisis 每 cycle 都推）
            if decision.get("regime") == "crisis":
                try:
                    from notifications import notify_crisis
                    notify_crisis(ticker, mkt.get("pct_chg", 0) or 0,
                                   reason=decision.get("reason", "")[:80])
                except Exception:
                    pass

            try:
                # WATCH_ONLY 标的只出信号，不下单
                if ticker not in WATCH_ONLY_TICKERS:
                    trade_execute(ticker, decision, mkt, window)
            except Exception as exc:
                logger.error(f"[trader ETF:{ticker}] error: {exc}")
        except Exception as exc:
            logger.error(f"[ETF:{ticker}] error: {exc}")


def _gold_cycle(macro: dict, window: str | None = None) -> None:
    from confluence import get_confluence
    from market_watch import get_shortterm_context
    try:
        from regime_today import get_today_regime
        board_regime = get_today_regime()
    except Exception:
        board_regime = None
    try:
        events   = get_gold_events_signal()
        mkt      = get_futures_signal(GOLD_TICKER)
        decision = get_gold_decision(mkt, events, macro, board_regime=board_regime)
        decision["session"] = _session()
        decision["confluence"] = get_confluence(mkt)

        # 15分钟K 短线扫描（GLD 同样适用）
        stx = get_shortterm_context(GOLD_TICKER)
        if stx:
            decision["m15_context"] = stx["m15_note"]
            decision["m15_direction"] = stx["m15_direction"]
            if (decision.get("action") == "HOLD"
                and stx["m15_score"] >= 2
                and stx["m15_direction"] in ("long", "short")):
                decision["m15_alert"] = (
                    f"日K中性，但15min触发短线"
                    f"{'做多' if stx['m15_direction'] == 'long' else '做空'}信号"
                )

        decision = apply_claude_gate(GOLD_TICKER, mkt, events, decision, macro, window)

        emit(mkt, events, decision)

        try:
            trade_execute(GOLD_TICKER, decision, mkt, window)
        except Exception as exc:
            logger.error(f"[trader GOLD] error: {exc}")
    except Exception as exc:
        logger.error(f"[GOLD:{GOLD_TICKER}] error: {exc}")


def _refresh_universe() -> None:
    """pre-open: 算 regime → 选今日卫星池 → 给新 picks 跑 evolver → 调 trader.apply_universe。

    架构原则（5-29 用户原话）：知道现在是哪种 regime，**根据这个为前提**给决策。
    """
    # Step 1: 算今日板块 regime（系统级单一源），写入 regime_state.json
    try:
        from regime_today import detect_and_save_regime, format_regime_banner
        info = detect_and_save_regime()
        for line in format_regime_banner(info):
            logger.info(line)
        regime = info["regime"]
    except Exception as exc:
        logger.error(t(f"[regime] 算定失败: {exc}, fallback neutral",
                       f"[regime] 算定失敗: {exc}, fallback neutral"))
        regime = "neutral"

    # Step 1b: HMM 自动 regime 检测 (Layer 3) — 与规则 regime 对照
    try:
        from hmm_regime import detect_today as hmm_detect, format_report as hmm_report
        hmm_info = hmm_detect()
        for line in hmm_report(hmm_info):
            logger.info(line)
        # 一致性检查：HMM 和规则 regime 若强烈矛盾发警告
        hmm_label = hmm_info.get("current_label", "")
        if hmm_label == "crisis" and regime not in ("crisis", "overheated"):
            logger.warning(t(f"⚠ HMM 判 crisis 但规则 {regime}，注意分歧",
                             f"⚠ HMM が crisis 判定だが規則は {regime}"))
        elif hmm_label in ("bear_or_correction",) and "bull" in regime:
            logger.warning(t(f"⚠ HMM 判 {hmm_label} 但规则 {regime}，注意分歧",
                             f"⚠ HMM が {hmm_label} 判定だが規則は {regime}"))
    except Exception as exc:
        logger.error(t(f"[hmm] 检测失败: {exc}", f"[hmm] 検出失敗: {exc}"))

    try:
        from universe_picker import pick_today_universe, format_picks_report
        from paper_trader import apply_universe
        picks_result = pick_today_universe(regime=regime)
        for line in format_picks_report(picks_result):
            logger.info(line)

        # 7c: 给"今日新 picks 但还没有 evolved_rules"的票跑一次 evolver
        try:
            from pathlib import Path
            from config import SIGNALS_DIR
            from strategy_evolver import run_evolver
            from strategy_engine import fetch_kline_history
            for p in (picks_result.get("picks") or []):
                tk = p["ticker_full"]
                rp = Path(SIGNALS_DIR) / f"evolved_rules_{tk.replace('.', '-')}.json"
                if rp.exists():
                    continue
                logger.info(t(f"  [evolver] 新 pick {tk} 无规则，启动进化...",
                              f"  [evolver] 新 pick {tk} ルールなし、進化開始..."))
                df = fetch_kline_history(tk)
                if df is None or len(df) < 80:
                    logger.warning(t(f"  [evolver] {tk} K线不足，跳过",
                                     f"  [evolver] {tk} K線不足、スキップ"))
                    continue
                survivors, _ = run_evolver(tk, df)
                logger.info(t(f"  [evolver] {tk} 进化完成: {len(survivors)} 条规则",
                              f"  [evolver] {tk} 進化完了: {len(survivors)} 件"))
        except Exception as exc:
            logger.error(t(f"[universe] evolver-on-new-picks 失败: {exc}",
                           f"[universe] evolver-on-new-picks 失敗: {exc}"))

        try:
            r = apply_universe(picks_result)
            logger.info(t(
                f"[universe] kept={len(r['kept'])} kicked={len(r['kicked'])} new={len(r['new'])}",
                f"[universe] kept={len(r['kept'])} kicked={len(r['kicked'])} new={len(r['new'])}",
            ))
            if r["kicked"]:
                logger.info(t(f"  踢出 (不在新 picks 且超容量): {r['kicked']}",
                              f"  追出 (新 picks 外で容量超過): {r['kicked']}"))
        except Exception as exc:
            logger.error(t(f"[universe] apply_universe 失败: {exc}",
                           f"[universe] apply_universe 失敗: {exc}"))
    except Exception as exc:
        logger.error(t(f"[universe] 选股失败: {exc}",
                       f"[universe] 銘柄選定失敗: {exc}"))


def _ensure_current_regime() -> None:
    """交易窗口兜底：regime 过期/缺失时先刷新系统级前提。"""
    try:
        from regime_today import get_today_info, detect_and_save_regime, format_regime_banner
        if get_today_info():
            return
        info = detect_and_save_regime()
        logger.info(t("[regime] 状态缺失或过期，已在当前交易窗口刷新。",
                      "[regime] 状態が欠落または期限切れのため、この実行枠で更新しました。"))
        for line in format_regime_banner(info):
            logger.info(line)
    except Exception as exc:
        logger.error(t(f"[regime] 兜底刷新失败: {exc}; 本轮使用 neutral",
                       f"[regime] フォールバック更新失敗: {exc}; 今回は neutral"))


def run_cycle(window: str | None = None) -> None:
    # 每个 cycle 起手刷一次 NAV peak (用于 paper_trader 的 drawdown floor)
    try:
        refresh_nav_peak()
    except Exception:
        pass
    if window and window != "pre-open":
        _ensure_current_regime()
    macro = _build_macro()
    equity_events = get_events_signal()
    # 关联个股财报 implied move：MU 财报前 30 天内自动注入到 events，
    # decision_agent._apply_earnings_guard 据此屏蔽 DRAM/MULL（必要时还有 SOXL/TQQQ→NVDA）
    try:
        from option_walls import (
            get_earnings_implied_move,
            _ETF_EARNINGS_LINKS,
            _next_earnings_for,
        )
        from config import LEVERAGE_FACTORS
        im_map: dict = {}
        for etf, related_stocks in _ETF_EARNINGS_LINKS.items():
            er = _next_earnings_for(etf)  # (stock, date, days_to) 或 None
            if er is None:
                continue
            stock, ed, _ = er
            try:
                em = get_earnings_implied_move(stock, ed)
            except Exception as ee:
                logger.error(t(f"earnings IM {stock} 失败: {ee}",
                               f"earnings IM {stock} 失敗: {ee}"))
                continue
            if em.get("error"):
                continue
            em["leverage"] = LEVERAGE_FACTORS.get(f"US.{etf}", 1.0)
            im_map[etf] = em
            im_map[f"US.{etf}"] = em
        if im_map:
            equity_events["earnings_implied_move"] = im_map
            parts = []
            for k, v in im_map.items():
                if k.startswith("US."):
                    continue
                im_pct = v.get("smoothed_implied_move_pct") or v.get("implied_move_pct") or 0
                parts.append(
                    f"{k}→{v.get('stock')}(T-{v.get('days_to_earnings')}, "
                    f"IM≈{im_pct:.1f}%×{v.get('leverage')})"
                )
            logger.info(t(f"  关联财报 IM: {', '.join(parts)}",
                          f"  関連決算 IM: ({len(parts)} 件)"))
    except Exception as exc:
        logger.error(t(f"关联财报 IM 注入失败: {exc}",
                       f"関連決算 IM 注入失敗: {exc}"))

    # 夜盘速览（开盘前最关键的领先指标）
    try:
        from nightwatch import format_nightwatch_lines
        for line in format_nightwatch_lines():
            logger.info(line)
    except Exception as exc:
        logger.error(t(f"夜盘速览失败: {exc}",
                       f"ナイトセッション速報失敗: {exc}"))

    # pre-open: 选今日卫星池（其它 window 不重选，picks 一日一固定）
    if window == "pre-open":
        _refresh_universe()

    # 核心仓 cycle
    _etf_cycle(equity_events, macro, window=window)
    _gold_cycle(macro, window=window)

    # 卫星仓 cycle：今日 picks + 任何还在持仓的卫星老仓 + WATCH_ONLY 观察标的
    try:
        from paper_trader import get_active_tickers, CORE_TICKERS
        actives = get_active_tickers()
        sats = [tk for tk in actives if tk not in CORE_TICKERS]
        # WATCH_ONLY 标的（只出信号不下单，_etf_cycle 内 skip trade_execute）
        sats = list(dict.fromkeys(sats + list(WATCH_ONLY_TICKERS)))
        if sats:
            _etf_cycle(equity_events, macro, window=window, tickers=sats)
    except Exception as exc:
        logger.error(t(f"卫星仓 cycle 失败: {exc}",
                       f"サテライト cycle 失敗: {exc}"))

    # 检查是否到了报告生成时间
    trigger = _report_trigger()
    if trigger:
        _run_report(trigger)


def _tick() -> None:
    """每5分钟检查一次是否进入日K交易窗口。"""
    window = _in_daily_window()
    if not window:
        return
    window_disp = {
        "pre-open":  t("开盘前扫描", "寄付前スキャン"),
        "midday":    t("午盘跟踪",   "前場フォロー"),
        "pre-close": t("收盘前确认", "引け前確認"),
    }
    logger.info(f"\n{'='*64}")
    logger.info(t(
        f"[{window_disp.get(window, window)}] 开始运行...",
        f"[{window_disp.get(window, window)}] 実行開始...",
    ))
    try:
        run_cycle(window=window)
        # 开盘报告：pre-open 触发；如果 pre-open 被 skip（机器休眠/进程重启），
        # post-open 也会 catch-up 生成一次，避免整天没有 AI 分析
        today = datetime.now(ET).date().isoformat()
        if window == "pre-open":
            _run_report("open")
            _last_report_session["open"] = today
        elif window == "post-open" and _last_report_session.get("open") != today:
            logger.info(t(
                "[补漏] pre-open 未触发 open 报告，post-open 补跑一次",
                "[補完] pre-open が open レポートを未生成、post-open で補完実行",
            ))
            _run_report("open")
            _last_report_session["open"] = today
    except Exception as exc:
        logger.exception(f"[scheduler] {window} 窗口执行失败，保留为未完成并等待重试: {exc}")
        return
    _mark_window_complete(window)


def main() -> None:
    _check_lock_or_exit()   # 单实例保护
    _record_startup_mtimes()   # 源码自愈：记录关键文件 mtime，运行时对比检测
    logger.info("=" * 64)
    logger.info(t("Trading Agents 启动（日K交易者模式）",
                  "Trading Agents 起動（日足トレーダーモード）"))
    logger.info(t(f"  ETF 标的 : {TICKERS}",
                  f"  ETF 銘柄: {TICKERS}"))
    logger.info(t(f"  黄金标的 : {GOLD_TICKER}",
                  f"  金 銘柄  : {GOLD_TICKER}"))
    logger.info(t(f"  运行窗口 : 08:30 pre-mkt / 09:20 pre-open / 10:00 post-open / 12:00 midday / 15:45 pre-close ET",
                  f"  実行ウィンドウ: 08:30 / 09:20 / 10:00 / 12:00 / 15:45 ET（1日5回）"))
    logger.info(t(f"  报告时间 : 开盘后 + 收盘后（ET 9:30 / 16:00）",
                  f"  レポート時刻: 寄付後 + 引け後（ET 9:30 / 16:00）"))
    logger.info(t(f"  60分钟K  : 可操作信号时自动附加入场参考",
                  f"  60分足  : 売買シグナル時に自動でエントリー参考付加"))
    # 决策模式
    from decision_agent import _is_technical_only
    if _is_technical_only():
        logger.info(t(
            "  决策模式 : [TECHNICAL_ONLY] 消息面（Trump/事件/breaking）仅作参考，不进决策",
            "  決定モード: [TECHNICAL_ONLY] ニュース面は参考のみ、決定に注入しない",
        ))
    else:
        logger.info(t(
            "  决策模式 : [FULL] 消息面 + 技术面综合",
            "  決定モード: [FULL] ニュース + テクニカル統合",
        ))
    logger.info("=" * 64)

    startup_window = _in_daily_window()
    if startup_window:
        logger.info(t(f"启动扫描：当前处于 {startup_window} 窗口，按窗口任务运行...",
                      f"起動スキャン：現在 {startup_window} ウィンドウ内、通常タスクとして実行..."))
        run_cycle(window=startup_window)
        if startup_window == "pre-open":
            _run_report("open")
        _mark_window_complete(startup_window)
    else:
        logger.info(t("启动扫描：非交易窗口，仅刷新信号，不生成开盘/收盘报告。",
                      "起動スキャン：取引ウィンドウ外のため、シグナル更新のみ。"))
        run_cycle()

    # 之后按ET窗口定时触发（5min 精度，防止 30min 间隔与 15min 窗口错开）
    schedule.every(5).minutes.do(_tick)

    try:
        while True:
            changed = _detect_code_change()
            if changed:
                _exit_for_code_change(changed)
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info(t("已停止。", "停止しました。"))
        sys.exit(0)


if __name__ == "__main__":
    main()
