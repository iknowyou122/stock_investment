# Phase 4.22：新引擎历史回测与准确率监控

**Goal:** 对比 v2.3 预突破引擎 vs v2.2 已确认突破引擎的历史表现，实现分层 A/B 测试框架

**Timeline:** Current sprint

---

## Task List

### Task 1: backtest_v23_vs_v22.py — 历史回测对比引擎
**Status:** Pending  
**Complexity:** Medium (integration task, multi-file coordination)  
**Model:** Standard (Sonnet)

**Requirements:**
- 在 `scripts/backtest_v23_vs_v22.py` 中实现
- 对比两个引擎：v2.2（当前 TripleConfirmationEngine）vs v2.3（新的预突破版本）
- 使用历史信号数据集（近 6 个月）进行回测
- 计算以下指标：
  - Win-rate（突破实现率）
  - False signal rate（假信号率）
  - Average upside（平均上涨幅度）
  - Time-to-breakout（从信号到实际突破的天数）
- 按产业分层显示结果
- 输出格式：Rich table + CSV
- 集成到 `make backtest-compare` 目标
- 参考 `scripts/coil_backtest.py` 和 `scripts/backtest.py` 的模式

**Dependencies:**
- TripleConfirmationEngine（已有 v2.3）
- 历史信号 CSV 文件（data/scans/scan_*.csv）
- 历史高价数据（OHLCV history）

**Acceptance Criteria:**
1. ✅ 脚本能够加载历史信号数据
2. ✅ 重新计算两个引擎的评分
3. ✅ 准确计算 win-rate 和 false signal rate
4. ✅ 按产业分层展示结果
5. ✅ 输出 CSV 供进一步分析
6. ✅ Make 集成 + help 说明更新

---

### Task 2: accuracy_monitor.py — 实时准确率追踪
**Status:** Pending  
**Complexity:** Medium (structured data tracking, state management)  
**Model:** Standard (Sonnet)

**Requirements:**
- 在 `scripts/accuracy_monitor.py` 中实现
- 追踪每日信号的实际突破情况
- 计算滚动 win-rate（最近 20/50/100 信号）
- 按产业、市值段、信心度分层统计
- 数据存储：SQLite 或 JSON 本地缓存（`config/signal_outcomes_cache.json`）
- 输出格式：Rich dashboard + CSV report
- 集成到 `make monitor` 目标
- 支持命令行参数：
  - `--date YYYY-MM-DD` 查询特定日期
  - `--industry 半導體` 按产业筛选
  - `--export report.csv` 导出报告

**Dependencies:**
- 历史信号数据库（signal_outcomes table 或 CSV cache）
- 最近突破数据（需要定期更新）

**Acceptance Criteria:**
1. ✅ 能读取历史信号并检查突破情况
2. ✅ 正确计算 win-rate（准确度高于 95%）
3. ✅ 支持分层统计（产业、市值、信心）
4. ✅ Dashboard 显示实时指标
5. ✅ CSV 导出功能正常
6. ✅ 缓存机制避免重复计算

---

### Task 3: ab_test_framework.py — 分层 A/B 测试框架
**Status:** Pending  
**Complexity:** Medium (statistical analysis, hypothesis testing)  
**Model:** Standard (Sonnet)

**Requirements:**
- 在 `scripts/ab_test_framework.py` 中实现
- 设计分层随机化框架（stratified randomization）
- 层级划分：
  - 按产业（半导体、电子、生技等）
  - 按市值（大/中/小）
  - 按 TAIEX 趋势（上升/中立/下降）
- 为每个信号随机分配：A 组（v2.2）或 B 组（v2.3）
- 计算统计显著性（t-test, chi-squared）
- 支持实时监控（每日更新）
- 输出：
  - HTML 报告（可视化分层结果）
  - 统计置信度指标
  - 建议（何时停止测试、哪个版本更好）

**Dependencies:**
- accuracy_monitor.py 输出
- backtest_v23_vs_v22.py 初始对标
- scipy.stats 统计库

**Acceptance Criteria:**
1. ✅ 正确实现分层随机化（重复性检验）
2. ✅ 统计显著性计算准确
3. ✅ HTML 报告格式清晰
4. ✅ 支持 `--confidence 0.95` 等参数
5. ✅ 集成到 `make ab-test` 目标

---

## Context & Patterns

**Referenced implementations:**
- `scripts/backtest.py`：回测框架（score replay、结果汇总）
- `scripts/coil_backtest.py`：蓄积回测（分层显示、指标计算）
- `scripts/coil_factor_report.py`：因子报告（Rich UI、CSV 导出）
- `scripts/analyze_outcomes.py`：win-rate 分析（数据验证）

**Data sources:**
- `data/scans/scan_*.csv`：历史信号
- `config/signal_outcomes.db` 或 CSV：实际突破记录
- OHLCV history：用于验证突破

**Configuration:**
- `config/engine_params.json`：v2.2 vs v2.3 参数对比
- 产业代号：1=半导体, 2=电子, 3=电机, 4=化工, ...

---

## Execution Notes

- **Branch strategy:** Work on a feature branch (e.g., `feature/phase-4-22-backtest`)
- **Testing:** Each task includes unit tests + integration tests
- **CI/CD:** All scripts must pass `make test` before merge
- **Documentation:** Update README.md and Makefile help text
- **Rollback plan:** If new engine performs worse, easy rollback via `config/engine_params.json`

