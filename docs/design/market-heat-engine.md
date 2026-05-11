# Market Heat Engine (Phase 4.25)

## 動機

`SurgeRadar` 和 `TIGHT_BASE` 在 v1 版本只看「個股本身的技術籌碼」，沒有問
「這支在當下市場的故事位階是什麼」。回測顯示同樣的緊壓基底，AI 供應鏈股票
D+1 命中率 6%+，傳產股票 0%。市場熱度是被忽略的關鍵變數。

## 四層架構

```
   國際/總經層       ← NVDA、SOX、TSM ADR、SMH、AVGO、SPY 隔夜變動
        ↓
   題材/概念層       ← 10 個策展概念股 basket（AI GPU、CPO、HBM、機器人...）
        ↓
   產業/個股層       ← TWSE 28 大類產業 1d/5d/20d 動量 + 廣度 + 領頭羊
        ↓
   LLM 統合          ← 上述三層 + 新聞 → 1-2 句市場主軸 + 主流題材排序
```

## 模組

### Layer 1: `domain/market_heat.py`

每日針對 28 大產業計算：

| 指標 | 說明 |
|------|------|
| `ret_1d/5d/20d_pct` | 平均產業漲幅 |
| `breadth_above_ma20_pct` | % 成分股在 MA20 上方 |
| `top5_vol_concentration` | 前 5 大成交金額占比（資金集中度）|
| `acceleration_pct` | `1d - 5d/5`（>0 加速中）|
| `rank_5d` | 1 = 最熱 |
| `rank_5d_change` | 與 5 天前比較（負數 = 輪入）|
| `leaders` | 前 3 大成交金額個股 + 漲跌 |

**市場狀態判斷：**
- `broad_rally`: 整體廣度 ≥ 70%
- `narrow_leadership`: 熱產業 ≥ 5 但廣度 < 50%
- `mixed`: 一般情況
- `broad_selloff`: 廣度 ≤ 30%

### Layer 2: `domain/concept_heat.py`

概念跨越產業界線。`config/concepts.json` 維護 10 個策展 basket：

```
AI_GPU_supply / CoWoS_advanced_packaging / CPO_silicon_photonics /
HBM_memory / AI_server_cooling / robotics_automation / low_orbit_satellite /
heavy_electric / auto_electronics / PCB_substrate
```

basket 動量計算邏輯與產業相同。

### Layer 3: `domain/international_signals.py`

抓取 8 個美股關鍵資產隔夜漲跌：

| 資產 | 對映台股 |
|------|----------|
| NVDA | AI_GPU_supply (+2), CoWoS (+2), HBM (+2), 半導體業 (+2) |
| AMD | AI_GPU_supply (+1), 半導體業 (+1) |
| ^SOX | 半導體業 (+2), AI_GPU_supply (+1) |
| TSM ADR | 半導體業 (+2), CoWoS (+2) |
| AVGO | CPO_silicon_photonics (+2) |
| ANET | CPO_silicon_photonics (+1) |
| SMH | 半導體業 (+1) |
| SPY | 金融保險業 (+1) |

`_classify_move(chg_pct)` 將漲跌映射為 -2 ~ +2 訊號，乘上 weight 後累加為
產業/概念順風分數，clamp 至 -3 ~ +3。

### LLM 整合: `domain/theme_analyzer.py`

組合上述三層資料 + 可選新聞 headlines，透過 LLM 產出：
- `narrative`: 1-2 句市場主軸
- `dominant_themes`: 排序後的主流題材
- `avoid_themes`: 建議避開
- `rotation_call`: 預測輪動方向
- `new_concept_suggestions`: 新題材建議（含候選股票）

無 LLM 時 fallback 為確定性輸出（產業排名前 3 + 概念排名前 3）。

## TIGHT_BASE v2 整合

```python
heat_bonus = 0

# 1. 產業熱度（rank_pct >= 80 → +5；60~80 → +3；< 40 直接淘汰）
if industry.rank_pct >= 80: heat_bonus += 5
elif industry.rank_pct >= 60: heat_bonus += 3
if industry.acceleration_pct > 0.5: heat_bonus += 2

# 2. 概念股 basket（任一熱門概念 → +3 each）
for concept in stock_concepts:
    if concept.rank_pct >= 70: heat_bonus += 3

# 3. 國際順風（產業 + 概念的順風分數加總）
heat_bonus += max(0, intl.industry_tailwind)
heat_bonus += max(0, intl.concept_tailwind)
```

**硬過濾**：產業 rank_pct < 40 的股票直接排除。

## 回測結果

90 天，648 支股票，53 個交易日：

| 版本 | 候選數 | D+1 命中 | D+1~3 命中 | Avg D-day |
|------|--------|----------|------------|-----------|
| v1 (no filter) | 1067 | 2.2% | 5.2% | +0.05% |
| v2 (heat-filt) | 722 | 2.8% | 5.8% | +0.14% |
| **v2 bonus ≥ 5** | **272** | **5.1%** | **10.3%** | **+0.33%** |
| **v2 bonus ≥ 6** | **81** | **6.2%** | **14.8%** | **+0.43%** |

**結論：熱度感知過濾 + bonus 讓命中率提升 2-3x。**

## 使用方式

```bash
# 每日盤後跑（D-1 晚上）
make heat-scan         # 純產業熱度排行
make pre-surge         # 完整 pipeline：熱度 + 概念 + 國際 + TIGHT_BASE + LLM
make pre-surge PRE_SURGE_MIN_BONUS=7   # 更嚴格的過濾

# 回測驗證
make tight-base-bt     # v1 baseline
make tight-base-v2     # 熱度感知版本
```

輸出位置：
- `data/market_heat/heat_YYYY-MM-DD.json` — 產業熱度
- `data/market_heat/concept_heat_YYYY-MM-DD.json` — 概念熱度
- `data/market_heat/intl_signals_YYYY-MM-DD.json` — 國際訊號
- `data/market_heat/theme_analysis_YYYY-MM-DD.json` — LLM 主軸
- `data/pre_surge_watchlist/watchlist_YYYY-MM-DD.json` — 明日候選

## 限制與待辦

1. **概念股 basket 需要持續維護**：產業會誕生新概念（如機器人代工、矽光子升級），
   需要人工或 LLM 半自動更新 `config/concepts.json`
2. **新聞層尚未實裝**：目前只用測試 headlines。後續應接入 Yahoo 股市 RSS
   或 FinMind News API
3. **國際訊號限於美股收盤後**：盤中即時順風偵測需要日內期貨資料
4. **回測樣本仍小**：90 天 × 5/天 = 272 候選，需累積到 500+ 才能更穩固結論
5. **未做 walk-forward 驗證**：當前回測使用同一段歷史，可能有 lookahead bias
   殘留（熱度排名是 D-1 收盤後計算的，但概念定義是事後策展）

## 後續優化方向

- 接 FinMind News API → 真實新聞驅動的題材偵測
- 把熱度 bonus 也加進 `SurgeRadar.score_full()` 作為新 factor
- 概念 basket 自動更新：LLM 每週掃讀新聞，建議調整成分股
- 加入產業內 5 日資金流（外資 + 投信買超分產業）
- 建立「主流題材變遷」歷史記錄，分析輪動週期
