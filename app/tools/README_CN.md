# 工具脚本说明（中文）

本目录用于放置可复用的工具脚本，按功能拆分，方便后续被业务模块或命令行直接调用。

运行方式（在项目根目录）：

```powershell
uv run python -m <模块路径> [参数]
```

例如：

```powershell
uv run python -m app.tools.cli.get_spot --code SZ300476
```

## 目录结构

- `app/tools/common.py`
  - 公共工具：关闭代理环境变量、创建 `StockService`、统一 JSON 输出。

- `app/tools/cli/`
  - `get_spot.py`：获取单只股票实时行情（多数据源自动切换）
  - `get_day_kline.py`：获取日 K
  - `get_week_kline.py`：获取周 K
  - `get_month_kline.py`：获取月 K
  - `get_minute_kline.py`：获取分钟线
  - `get_company_info.py`：获取公司资料
  - `get_money_flow.py`：获取资金流
  - `get_chip_distribution.py`：获取筹码分布
  - `get_market_indices.py`：获取主要指数快照

- `app/tools/strategy/`
  - `check_strategy.py`：执行单票策略检测
  - `list_strategies.py`：列出已保存策略
  - `run_backtest.py`：执行回测

- `app/tools/favorites/`
  - `add_favorite.py`：新增自选
  - `get_favorites.py`：查询自选列表
  - `remove_favorite.py`：移除自选

- `app/tools/monitor/`
  - `run_monitor.py`：执行监控流程
  - `test_dingtalk_notify.py`：发送钉钉测试提醒

- `app/tools/sources/`
  - `get_xueqiu_quote.py`：仅调用雪球数据源拉取行情

- `app/tools/benchmark_spot_sources.py`
  - 全量与单票行情性能/稳定性压测脚本。

- `app/tools/demo_dingtalk_notify.py`
  - 钉钉测试接口 demo 脚本。

## 常用命令示例

```powershell
uv run python -m app.tools.cli.get_spot --code SZ300476
uv run python -m app.tools.cli.get_day_kline --code SZ300476 --start 2025-01-01 --end 2025-12-31

## 补充说明（最近更新）

- 监控支持 `monitor_kline_frequency` 配置（默认 `d` 日K）。
- 自选股涨跌幅字段由后端统一归一化，缺失时会使用 `price/prev_close` 自动回推。
uv run python -m app.tools.strategy.check_strategy --code SZ300476 --shares 100 --cost 300
uv run python -m app.tools.monitor.test_dingtalk_notify
uv run python -m app.tools.benchmark_spot_sources --sample-size 100 --timeout-sec 2
```

## 维护约定

- 临时验证脚本用完即删，不长期保留；回归断言沉淀到 `tests/`（unittest）。
- 稳定且可复用的脚本再同步到 `app/tools/`。
- 每个脚本尽量只做一件事，输入参数清晰，输出统一 JSON，方便被其他模块当“工具类”调用。
