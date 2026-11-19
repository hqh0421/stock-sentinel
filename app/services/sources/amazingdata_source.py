"""星耀数智(AmazingData) 官方 Python SDK 适配器。

用于替代/增强项目的实时行情主数据源。通过官方 `AmazingData` SDK
(`import AmazingData as ad`) 的 `MarketData.query_snapshot` 获取 Level-1 快照，
返回与项目规范一致的字段字典。

重要：星耀 SDK(tgw) 不是线程安全的——在 worker 线程中调用 `query_snapshot`
会静默返回空（其底层依赖主线程的 asyncio 事件循环）。因此本适配器把真正的
SDK 调用放进一个常驻的【子进程】中执行（子进程拥有独立的主线程与事件循环），
从任意调用线程（CLI 主线程 / FastAPI 线程池 / 监控线程）调用均安全。

说明：
- 官方 SDK 不在公共 PyPI，需从 gitee 安装 wheel（tgw + AmazingData），
  与本仓库 tools/market_data_tool 等数据源平级。
- 快照字段里没有 name / change / pct_chg，name 由调用方用本地缓存补齐，
  change / pct_chg 由 last 与 pre_close 计算（与 StockService._normalize_spot_quote 一致）。
- 同一账号不建议同时登录多处（MCP 与本项目二选一），否则会被服务端互踢。
"""

import atexit
import datetime
import logging
import multiprocessing as mp
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger("app.market_data")

# K线周期映射（AmazingData.constant.Period 枚举值）
AD_PERIOD_MAP: Dict[str, int] = {
    "d": 10008, "w": 10009, "m": 10010,
    "1": 10000, "3": 10001, "5": 10002, "10": 10003, "15": 10004,
    "30": 10005, "60": 10006, "120": 10007,
    "minute": 10000, "1d": 10000, "5d": 10000,
}


def disable_local_proxy_env() -> None:
    """强制所有出站请求绕过本地/系统代理（与 app.main.disable_local_proxy_env 行为一致）。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# 子进程内的 SDK worker（常驻进程，登录一次后复用会话）
# ---------------------------------------------------------------------------
_worker_tool: Optional["AmazingDataTool"] = None
_pool: Optional[mp.Pool] = None
_pool_lock = threading.Lock()
_pool_ready = threading.Event()  # SDK 登录初始化完成前，调用方直接走降级数据源（暖机约 30~40s）
_pool_cooldown_until = 0.0  # 暖机连续失败熔断：冷却期内不再重建池（防互踢死循环冲击内存）
_POOL_COOLDOWN_SEC = 900.0
# 子进程会复制父进程的地址空间，星耀 SDK 登录还要再吃一块内存。
# 容器 1GB 上限下，余量不足就干脆别建池——否则 OOM killer 杀掉的是 uvicorn 主进程，
# 表现是"整个服务重启"，比"拿不到星耀行情"严重得多。
_MIN_POOL_HEADROOM_MB = 350.0
_last_skip_log = 0.0


def _cgroup_memory_headroom_mb() -> Optional[float]:
    """容器内存余量（MB）。拿不到（非 cgroup 环境）返回 None，表示不做限制。"""
    try:  # cgroup v2
        with open("/sys/fs/cgroup/memory.current") as f:
            cur = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.max") as f:
            mx_raw = f.read().strip()
        if mx_raw != "max":
            return (int(mx_raw) - cur) / 1048576.0
    except Exception:
        pass
    try:  # cgroup v1
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
            cur = int(f.read().strip())
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            mx = int(f.read().strip())
        if mx < (1 << 60):
            return (mx - cur) / 1048576.0
    except Exception:
        pass
    return None


def _pool_in_cooldown() -> bool:
    import time as _time
    return _time.time() < _pool_cooldown_until


def pool_ready() -> bool:
    """常驻池是否已暖机完成（可立即取数）。

    兜底方在调用星耀之前必须先问这一句：池未就绪时 `fetch_spot_quotes`
    会立刻返回空，等于白跑一趟；而第一次调用只能触发异步暖机（实测 9~17s），
    当场是拿不到数的。所以「能不能兜底」= 这里是否为 True。
    """
    return _pool_ready.is_set() and not _pool_in_cooldown()


def warmup_async(username: str, password: str, host: str, port: int) -> bool:
    """应用启动时后台暖机，让兜底能力在真正需要之前就绪。

    返回 True 表示已发起（不代表成功）。失败会静默冷却 15 分钟，不会抛。
    注意：登录会把同一账号在别处（如 MCP、本机）的会话踢下线，
    因此是否在启动时暖机由调用方决定，本函数不做判断。
    """
    try:
        pool = _get_pool(username, password, host, port)
        return pool is not None
    except Exception as exc:  # noqa: BLE001
        _logger.warning("amazingdata_warmup_request_failed error=%r", exc)
        return False


def _ad_worker_ping() -> bool:
    """暖机探针：确认子进程内 SDK 会话就绪。"""
    if _worker_tool is None:
        return False
    try:
        _worker_tool._ensure_session()
        return bool(_worker_tool._ready)
    except Exception:
        return False


def _start_warmup(pool: "mp.Pool") -> None:
    global _pool_cooldown_until

    def _run():
        try:
            ok = pool.apply_async(_ad_worker_ping).get(timeout=180)
            if ok:
                _pool_ready.set()
                _logger.info("amazingdata_pool_warmup_ok")
            else:
                _logger.warning("amazingdata_pool_warmup_failed ping=False，冷却 %ss", int(_POOL_COOLDOWN_SEC))
                with _pool_lock:
                    import time as _time
                    _pool_cooldown_until = _time.time() + _POOL_COOLDOWN_SEC
                _reset_pool()  # 登录失败，冷却后由下次调用重试
        except Exception as exc:
            _logger.warning("amazingdata_pool_warmup_error error=%r，冷却 %ss", exc, int(_POOL_COOLDOWN_SEC))
            with _pool_lock:
                import time as _time
                _pool_cooldown_until = _time.time() + _POOL_COOLDOWN_SEC
            _reset_pool()

    threading.Thread(target=_run, daemon=True, name="ad-warmup").start()


def _with_alarm(seconds: float, fn, *args):
    """在子进程主线程内给 SDK 调用加硬超时，防止挂死占用唯一 worker。"""
    import signal
    use_alarm = True
    try:
        def _handler(signum, frame):
            raise TimeoutError("amazingdata_query_timeout")

        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(max(1, int(seconds)))
    except Exception:
        use_alarm = False
    try:
        return fn(*args)
    finally:
        if use_alarm:
            signal.alarm(0)


def _ad_worker_init(username: str, password: str, host: str, port: int) -> None:
    global _worker_tool
    disable_local_proxy_env()
    _worker_tool = AmazingDataTool(username=username, password=password, host=host, port=port, timeout_sec=10.0)
    try:
        _worker_tool._ensure_session()
    except Exception:
        _worker_tool = None


def _ad_worker_fetch(task: Tuple[List[str], List[int]]) -> Dict[str, Dict[str, Any]]:
    ad_codes, candidate_dates = task
    if _worker_tool is None:
        return {}
    for _attempt in (0, 1):
        try:
            def _run():
                for date_int in candidate_dates:
                    try:
                        res = _worker_tool._query_date(ad_codes, date_int)
                    except Exception:
                        res = {}
                    if res:
                        return res
                return {}

            res = _with_alarm(30, _run)
            if res:
                return res
            return {}
        except Exception:
            if _attempt == 0 and _worker_tool is not None:
                try:
                    _worker_tool._ready = False
                    _worker_tool._ensure_session()
                except Exception:
                    return {}
    return {}


def _ad_worker_kline(task: Tuple[List[str], int, int, int]) -> Dict[str, List[Dict[str, Any]]]:
    ad_codes, begin_int, end_int, period_int = task
    if _worker_tool is None:
        return {}
    for _attempt in (0, 1):
        try:
            try:
                res = _with_alarm(30, _worker_tool._query_kline, ad_codes, begin_int, end_int, period_int)
            except Exception:
                res = {}
            if res:
                return res
            return {}
        except Exception:
            if _attempt == 0 and _worker_tool is not None:
                try:
                    _worker_tool._ready = False
                    _worker_tool._ensure_session()
                except Exception:
                    return {}
    return {}


def _reset_pool() -> None:
    global _pool
    # 锁内只做「摘引用」，销毁必须在锁外：terminate/join 可能因 worker（tgw 原生
    # 线程库）吞掉 SIGTERM 而长时间不返回。若在锁内销毁，一旦 worker 卡死，
    # 所有要池的请求会永久阻塞在 _pool_lock 上（2026-11-03 11:34 实际事故：
    # 4 次 pool_timeout 连锁触发 _reset_pool，favorites 接口整体挂死）。
    with _pool_lock:
        stale, _pool = _pool, None
        _pool_ready.clear()
    if stale is not None:
        _destroy_pool(stale)


def _destroy_pool(pool: "mp.Pool") -> None:
    """销毁进程池，等待有上界：terminate 后 worker 5s 不退就 SIGKILL 强杀。"""
    t0 = time.time()
    try:
        pool.terminate()
    except Exception:
        pass
    for p in (getattr(pool, "_pool", None) or []):
        try:
            p.join(timeout=5)
            if p.is_alive():
                _logger.warning("amazingdata_worker_force_kill pid=%s（terminate 后 5s 未退）", p.pid)
                p.kill()
                p.join(timeout=3)
        except Exception:
            pass
    try:
        pool.join()
    except Exception:
        pass
    _logger.info("amazingdata_pool_destroyed elapsed=%.1fs", time.time() - t0)


def _get_pool(username: str, password: str, host: str, port: int) -> Optional[mp.Pool]:
    global _pool, _last_skip_log
    # 限时获取：即使未来有人在锁内引入慢操作，也只是本次降级（走腾讯等兜底），
    # 而不是把调用线程永久挂死（2026-11-03 事故教训）。
    if not _pool_lock.acquire(timeout=3):
        if time.time() - _last_skip_log > 60:
            _last_skip_log = time.time()
            _logger.warning("amazingdata_pool_lock_busy（池重建/销毁进行中，本次降级走兜底源）")
        return None
    try:
        if _pool_in_cooldown():
            return None  # 熔断冷却期内不再重建池（账号可能在别处被登录/互踢）
        if _pool is None:
            headroom = _cgroup_memory_headroom_mb()
            if headroom is not None and headroom < _MIN_POOL_HEADROOM_MB:
                if time.time() - _last_skip_log > 600:
                    _last_skip_log = time.time()
                    _logger.warning(
                        "amazingdata_pool_skip_low_memory headroom=%.0fMB < %.0fMB（容器内存不足，"
                        "星耀本次不启用，现价由腾讯/雪球等源承担）", headroom, _MIN_POOL_HEADROOM_MB)
                return None
            _pool = mp.Pool(processes=1, initializer=_ad_worker_init, initargs=(username, password, host, port))
            _pool_ready.clear()
            _start_warmup(_pool)  # 后台暖机，完成后调用方才走星耀
        return _pool
    finally:
        _pool_lock.release()


# 进程退出时清理常驻子进程，避免 ResourceWarning
atexit.register(_reset_pool)


class AmazingDataTool:
    def __init__(
        self,
        username: str,
        password: str,
        host: str,
        port: int,
        timeout_sec: float = 10.0,
    ) -> None:
        self._username = username
        self._password = password
        self._host = host
        self._port = int(port)
        self._timeout = timeout_sec
        self._lock = threading.Lock()
        self._ready = False
        self._ad = None
        self._md = None
        self._md_date = None

    # ---------- 代码格式转换 ----------
    @staticmethod
    def to_ad_code(code: str) -> str:
        """项目代码(如 SH600519 / sz000001 / 600519.SH) -> 星耀数智格式 600519.SH。"""
        s = str(code or "").strip().upper()
        if s[:2] in ("SH", "SZ", "BJ"):
            s = s[2:]
        digits = "".join(ch for ch in s if ch.isdigit())[-6:]
        if not digits:
            return ""
        market = "SH" if digits.startswith(("6", "9")) else ("BJ" if digits.startswith(("4", "8")) else "SZ")
        return f"{digits}.{market}"

    @staticmethod
    def _digits(ad_code: str) -> str:
        return str(ad_code).split(".")[0]

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _today_int() -> int:
        return int(datetime.date.today().strftime("%Y%m%d"))

    def is_ready(self) -> bool:
        """进程池是否已暖机（登录初始化完成）。未就绪时 fetch 返回空属正常降级，不算失败。"""
        return _pool is not None and _pool_ready.is_set()

    def prewarm(self) -> bool:
        """启动即建池并后台暖机，让首个用户请求不必等 9 秒登录。"""
        try:
            pool = _get_pool(self._username, self._password, self._host, self._port)
            if pool is None:
                return False
            _logger.info("amazingdata_prewarm_started warmup_in_background=1")
            return True
        except Exception as exc:
            _logger.warning("amazingdata_prewarm_failed error=%r", exc)
            return False

    # ---------- 会话管理（仅在子进程内调用） ----------
    def _ensure_session(self, timeout_sec: float = 20.0) -> None:
        with self._lock:
            if self._ready:
                return
            disable_local_proxy_env()  # 确保 SDK 出站不走本地/系统代理
            # SDK 的 login 在会话被占用/服务端异常时可能无限阻塞（实测会挂死），
            # 用 SIGALRM 加硬超时，保证失败快速返回而不是毒死常驻子进程。
            import signal
            import threading
            use_alarm = threading.current_thread() is threading.main_thread()

            def _login_timeout(signum, frame):
                raise TimeoutError("amazingdata_login_timeout")

            if use_alarm:
                try:
                    signal.signal(signal.SIGALRM, _login_timeout)
                    signal.alarm(int(timeout_sec))
                except Exception:
                    use_alarm = False
            try:
                import AmazingData as ad  # 延迟导入：未安装时不影响其它数据源

                ad.login(
                    username=self._username,
                    password=self._password,
                    host=self._host,
                    port=self._port,
                )
                self._ad = ad
                self._ready = True
            finally:
                if use_alarm:
                    signal.alarm(0)

    def _query_date(self, ad_codes: List[str], date_int: int) -> Dict[str, Dict[str, Any]]:
        if self._md_date != date_int or self._md is None:
            self._md = self._ad.MarketData([date_int])
            self._md_date = date_int
        raw = self._md.query_snapshot(ad_codes, begin_date=date_int, end_date=date_int)
        # 兼容两种返回形态：{date:{code:df}} 或 ([dict,err], {date:{code:df}})
        data = raw[1] if isinstance(raw, tuple) and len(raw) == 2 else raw
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for _dk, code_map in data.items():
            if not isinstance(code_map, dict):
                continue
            for ad_code, df in code_map.items():
                if df is None or len(df) == 0:
                    continue
                row = df.iloc[-1]  # 快照为日内序列，取最后一行即最新
                digits = self._digits(ad_code)
                out[digits] = self._row_to_quote(row, digits)
        return out

    def _row_to_quote(self, row, digits: str) -> Dict[str, Any]:
        price = self._to_float(row.get("last"))
        prev = self._to_float(row.get("pre_close"))
        change = (price - prev) if (price is not None and prev not in (None, 0)) else None
        pct = (change / prev * 100) if (change is not None and prev not in (None, 0)) else None
        return {
            "code": digits,
            "name": None,  # 由调用方用本地缓存补齐
            "price": price,
            "prev_close": prev,
            "open": self._to_float(row.get("open")),
            "high": self._to_float(row.get("high")),
            "low": self._to_float(row.get("low")),
            "volume": self._to_float(row.get("volume")),
            "amount": self._to_float(row.get("amount")),
            "change": change,
            "pct_chg": pct,
            "source": "amazingdata",
            "trade_time": str(row.get("trade_time")) if row.get("trade_time") is not None else None,
        }

    # ---------- K线 / 分时（query_kline，运行在子进程内） ----------
    @staticmethod
    def _pick_field(row: Any, names: Tuple[str, ...]) -> Any:
        for n in names:
            if isinstance(row, dict):
                v = row.get(n)
            else:
                v = getattr(row, n, None)
            if v is not None:
                return v
        return None

    @classmethod
    def _kline_items(cls, obj: Any) -> List[Any]:
        """SDK 返回的 df / list / 模型列表 -> 可迭代的行对象列表。"""
        if obj is None:
            return []
        if isinstance(obj, list):
            return obj
        try:
            import pandas as pd
            if isinstance(obj, pd.DataFrame):
                return list(obj.to_dict("records"))
        except Exception:
            pass
        return [obj]

    @staticmethod
    def _norm_kline_time(value: Any) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip()
        digits = "".join(ch for ch in s if ch.isdigit())
        if s and s == digits:
            if len(digits) == 8:  # 20261102
                return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            if len(digits) >= 12:  # 20261102 093000 / 20261102093000
                return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:00"
        return s

    def _normalize_kline_row(self, row: Any) -> Optional[Dict[str, Any]]:
        t = self._pick_field(row, ("kline_time", "time", "datetime", "trade_time", "date", "bar_time", "timestamp"))
        close = self._pick_field(row, ("close", "last", "close_price", "last_price"))
        if t is None or close is None:
            return None
        date_text = self._norm_kline_time(t)
        if not date_text:
            return None
        return {
            "date": date_text,
            "open": self._to_float(self._pick_field(row, ("open", "open_price"))),
            "high": self._to_float(self._pick_field(row, ("high", "high_price"))),
            "low": self._to_float(self._pick_field(row, ("low", "low_price"))),
            "close": self._to_float(close),
            "volume": self._to_float(self._pick_field(row, ("volume", "vol", "volume_qty"))),
            "amount": self._to_float(self._pick_field(row, ("amount", "turnover", "money", "value"))),
        }

    def _query_kline(self, ad_codes: List[str], begin_int: int, end_int: int, period_int: int) -> Dict[str, List[Dict[str, Any]]]:
        self._ensure_session()
        self._md = self._ad.MarketData([begin_int])
        self._md_date = begin_int
        raw = self._md.query_kline(ad_codes, begin_date=begin_int, end_date=end_int, period=period_int)
        # 兼容多种返回形态：tuple 包装 / {code:df|list} / {date:{code:df}} / list / DataFrame
        data = raw[1] if isinstance(raw, tuple) and len(raw) >= 2 else raw
        out: Dict[str, List[Dict[str, Any]]] = {}

        def add(digits: str, items: List[Any]) -> None:
            rows = [r for r in (self._normalize_kline_row(it) for it in items) if r]
            if rows:
                out.setdefault(digits, []).extend(rows)

        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, dict):
                    for code2, df in val.items():
                        add(self._digits(str(code2)), self._kline_items(df))
                else:
                    add(self._digits(str(key)), self._kline_items(val))
        elif isinstance(data, list):
            for item in data:
                code2 = self._pick_field(item, ("code", "symbol", "security_code", "security_id"))
                if not code2:
                    continue
                sub = self._pick_field(item, ("data", "klines", "kline_list", "list", "bars"))
                add(self._digits(str(code2)), self._kline_items(sub))
        elif data is not None and len(ad_codes) == 1:
            # 无法识别代码归属的扁平结构，且只查了一只股票
            add(self._digits(ad_codes[0]), self._kline_items(data))
        return out

    def fetch_kline(
        self,
        code_list: List[str],
        begin_date: Any,
        end_date: Any,
        period_key: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """K线/分时查询（周期: d/w/m/1/3/5/10/15/30/60/120/1d/5d）。

        返回 {6位代码: [{date,open,high,low,close,volume,amount}, ...]}，
        失败 / 无权限 / 无数据返回 {}（调用方据此走降级数据源）。
        真正的 SDK 调用在常驻子进程中执行（线程安全）。
        """
        period_int = AD_PERIOD_MAP.get(str(period_key or "").strip().lower())
        if not period_int:
            return {}
        ad_codes = [self.to_ad_code(c) for c in code_list if c]
        ad_codes = [c for c in ad_codes if c]
        if not ad_codes:
            return {}

        def _to_int_date(value: Any, fallback: int) -> int:
            digits = "".join(ch for ch in str(value or "") if ch.isdigit())
            return int(digits[:8]) if len(digits) >= 8 else fallback

        today_int = self._today_int()
        begin_int = _to_int_date(begin_date, today_int)
        end_int = _to_int_date(end_date, today_int)
        if begin_int > end_int:
            begin_int, end_int = end_int, begin_int
        try:
            pool = _get_pool(self._username, self._password, self._host, self._port)
            if pool is None or not _pool_ready.is_set():
                return {}  # 未暖机：立即降级，不阻塞调用方
            result = pool.apply_async(
                _ad_worker_kline, ((ad_codes, begin_int, end_int, period_int),)
            ).get(timeout=self._timeout + 12)
            return result or {}
        except mp.TimeoutError:
            _logger.warning("amazingdata_kline_pool_timeout period=%s", period_key)
            _reset_pool()
            return {}
        except Exception as exc:
            _logger.warning("amazingdata_kline_pool_error period=%s error=%r", period_key, exc)
            return {}

    # ---------- 对外接口 ----------
    def fetch_spot_quotes(
        self, code_list: List[str], trade_date: Optional[int] = None,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """批量获取实时快照，返回 {6位代码: 规范字段字典}。

        真正的 SDK 调用在常驻子进程中执行（线程安全）。trade_date 缺省为今天；
        若当天无数据(周末/休市)会向前回溯最近的交易候选日（仅工作日，最多 5 个），
        以保证监控场景总能取到最近一个交易日的快照。
        timeout_sec：等待子进程结果的上限。报价路径应传短值（如 8s），
        worker 挂死/被互踢时快速降级，别让每只票干等 22s；K线等重查询用缺省长超时。
        """
        ad_codes = [self.to_ad_code(c) for c in code_list if c]
        ad_codes = [c for c in ad_codes if c]
        if not ad_codes:
            return {}
        target = trade_date or self._today_int()
        base = datetime.date(target // 10000, (target // 100) % 100, target % 100)
        candidate_dates: List[int] = []
        cur = base
        # 仅回退到最近的交易候选日（跳过周末），最多 5 个，减少无效快照请求与时延
        while len(candidate_dates) < 5 and (base - cur).days < 30:
            if cur.weekday() < 5:  # 周一~周五
                candidate_dates.append(int(cur.strftime("%Y%m%d")))
            cur = cur - datetime.timedelta(days=1)
        if not candidate_dates:
            candidate_dates = [target]
        try:
            pool = _get_pool(self._username, self._password, self._host, self._port)
            if pool is None or not _pool_ready.is_set():
                return {}  # 未暖机：立即降级，不阻塞调用方
            result = pool.apply_async(_ad_worker_fetch, ((ad_codes, candidate_dates),)).get(
                timeout=timeout_sec if timeout_sec else self._timeout + 12
            )
            return result or {}
        except mp.TimeoutError:
            _logger.warning("amazingdata_pool_timeout")
            _reset_pool()
            return {}
        except Exception as exc:
            _logger.warning("amazingdata_pool_error error=%r", exc)
            return {}
