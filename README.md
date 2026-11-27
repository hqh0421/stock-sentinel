# Stock Sentinel

自托管的 A 股监控与决策支持系统：行情监控、策略回测、交易记账、绩效复盘，
以及一个基于 ReAct 模式的 AI 分析 Agent。本地 / 私有服务器运行，数据完全自持。

> 仅供学习与研究，不构成任何投资建议。

## 功能特性

- **实时行情**：多数据源自动降级（腾讯 → 雪球 → 东方财富 → AkShare 快照 → 可选的星耀数智兜底），带熔断与健康度展示
- **自选股管理**：分组、备注、实时行情表、分时迷你图、个股详情抽屉（K 线 / 指标 / 持仓 / 策略信号）
- **监控与通知**：服务端调度器按交易时段巡检（关掉浏览器也会推送），钉钉 Webhook 通知；信号可经 AI 复核后推送
- **策略与回测**：表达式策略（AST 白名单安全求值）、因子有效性检验（IC/ICIR、分层回测、样本外 walk-forward）、次日开盘撮合回测（含费率/滑点/复利净值）、组合风控（区间位置 / 波动率目标 / 回撤护栏 / 集中度）
- **交易记账**：逐笔记账（买入摊薄成本、卖出回收成交额）、已实现盈亏汇总、绩效复盘（净值曲线 / 最大回撤 / 夏普 / 胜率 / 沪深300 超额）
- **AI Agent**：轻量 ReAct 层，30+ 语义工具转调现有服务，6 个场景（分析顾问 / 信号复核 / 盘后复盘 / 卖出决策 / 策略诊断 / 持仓风险），SSE 流式输出工具调用轨迹；买入/卖出类结论强制先取证再下结论
- **多用户**：账号隔离的自选 / 持仓 / 流水 / 设置 / 策略（SQLite），登录防爆破（全局失败计数 + 指数退避）
- **运维**：数据库每日热备与一键恢复、告警历史、操作审计、数据源熔断告警

## 架构一览

```text
app/
  main.py                    # FastAPI 入口与中间件门禁
  routers/                   # API 路由（stock / company / auth / agent / ...）
  services/                  # 业务服务（行情聚合、回测、记账、监控调度、风控 ...）
  agent/                     # ReAct 运行时、场景提示词、工具注册表
  core/                      # 决策规则单一事实源、表达式安全求值、鉴权
  tools/                     # 可复用 CLI 工具脚本（行情 / 监控 / 自选）
frontend/                    # 单页看板（原生 JS，无构建步骤）
config/                      # *.example.json 模板 + 运行时生成/挂载的配置
docker/                      # Dockerfile 与 compose（本地运行）
tests/                       # unittest 用例（标准库，无额外测试依赖）
```

后端 FastAPI + SQLite；前端原生 JS 单页；全部数据（行情缓存、用户态、告警历史）落在本地 `config/` 与 SQLite，可整目录备份。

## 快速开始

### Windows 免安装绿色版（推荐给普通用户）

到 [Releases](https://github.com/hqh0421/stock-sentinel/releases) 下载
`stock-sentinel-portable-win64-*.zip`，解压后双击 **`start_portable.bat`**：

- 内嵌 Python 运行时，**不需要**安装 Python 或 Docker
- 首次运行自动生成 `.env`（含随机登录口令，可用记事本改）
- 服务就绪后自动打开浏览器 `http://localhost:8888/`；关闭窗口即停止服务
- 全部数据（SQLite、配置、日志）都在解压目录内，搬走目录即完成备份

### Docker（服务器 / 长期运行推荐）

```bash
# 仓库根目录放一份 .env（至少一行 APP_PASSWORD=你的口令），然后：
docker compose -f docker/docker-compose.local.yml up -d --build
```

访问 `http://localhost:8888/`，用 `APP_PASSWORD` 登录。

### 直接运行（Windows + uv）

```bat
start.bat                    # 默认 127.0.0.1:8888，可传参 HOST PORT
```

### 配置

- `cp config/models.example.json config/models.json` 后填入你的 LLM API Key（OpenAI 兼容接口即可，模型名可换）
- 环境变量优先级最高且不会被写回磁盘：
  `MONITOR_STOCK_LLM_API_KEY` / `MONITOR_STOCK_DINGTALK_WEBHOOK_URL` / `MONITOR_STOCK_DINGTALK_SECRET` / `MONITOR_STOCK_XUEQIU_COOKIE` 等，见 `.env.example`
- 用户态数据存 SQLite `config/app.db`，首个注册用户为管理员；推荐用 CLI 建号：

```bash
python -m app.tools.cli.create_user alice
```

### 关于星耀数智（AmazingData）SDK

`app/services/sources/amazingdata_source.py` 是星耀数智官方 SDK 的可选适配器。
该 SDK 为商业授权、不在公共 PyPI，**本仓库不附带其 wheel**，删除
`docker/Dockerfile.local` 中安装 wheels 的两行也不影响构建与运行——
行情链路会自动降级到 腾讯 → 雪球 → 东财 → AkShare 兜底（SDK 导入为延迟加载，未安装时不影响其它数据源）。

## 开发与测试

```bash
python -m compileall -q app scripts tests          # 编译检查
python -m unittest discover -s tests -t .          # 单元测试（标准库 unittest）
python scripts/verify_startup.py                   # 完整应用启动冒烟（隔离临时库）
python scripts/harden_secrets.py                   # 密钥泄露只读自检
```

测试在包导入阶段即把数据库路径重定向到临时目录，隔离是框架级的，不依赖人肉约定。

## 安全说明

- 登录口令来自环境变量 `APP_PASSWORD`；会话为 HMAC 签名 Cookie，改口令即吊销全部旧会话
- 登录失败全局计数 + 指数退避（5 次后 429），口令校验在线程池执行
- 敏感配置（模型 Key、钉钉 Webhook、雪球 Cookie）建议全部走环境变量；`scripts/harden_secrets.py` 可只读自检仓库中是否有密钥残留
- 默认纯 HTTP，对外暴露请自行加反向代理 + HTTPS

## License

[MIT](LICENSE)
