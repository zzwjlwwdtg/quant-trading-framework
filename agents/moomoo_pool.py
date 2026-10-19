"""
moomoo 连接池 — 全局单例 OpenQuoteContext。
避免每次 get_market_signal 都新建/关闭连接，减少 OpenD 端的资源压力和启动延迟。
"""

import atexit
import threading
import time

from moomoo import OpenQuoteContext, RET_OK
from config import OPEND_HOST, OPEND_PORT

_ctx = None
_lock = threading.Lock()


def _is_alive(ctx) -> bool:
    """ping 检查连接是否还活着（get_global_state 是最轻的接口）。"""
    try:
        ret, _ = ctx.get_global_state()
        return ret == RET_OK
    except Exception:
        return False


def get_quote_ctx(retries: int = 5, delay: float = 2.0) -> OpenQuoteContext:
    """
    返回共享 OpenQuoteContext。OpenD 没启动时重试 retries 次。
    线程安全；连接断开后自动重建。
    """
    global _ctx
    with _lock:
        if _ctx is not None and _is_alive(_ctx):
            return _ctx

        # 重建
        if _ctx is not None:
            try: _ctx.close()
            except Exception: pass
            _ctx = None

        last_err = None
        for i in range(retries):
            try:
                _ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
                # 立即 ping 验证连接确实建立
                if _is_alive(_ctx):
                    return _ctx
                _ctx.close()
                _ctx = None
            except Exception as e:
                last_err = e
            time.sleep(delay)
        raise ConnectionError(
            f"moomoo OpenD 不可达 {OPEND_HOST}:{OPEND_PORT} (重试{retries}次)。"
            f" 请确认 OpenD GUI 已启动并登录。Last error: {last_err}"
        )


@atexit.register
def close_quote_ctx() -> None:
    """Close the shared quote context now; safe to call more than once.

    Short-lived commands must call this explicitly before returning. Relying
    only on ``atexit`` can leave the process waiting on moomoo worker threads,
    so the batch file never reaches its remaining commands.
    """
    global _ctx
    with _lock:
        ctx = _ctx
        _ctx = None
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
