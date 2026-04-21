# Signal Engine Design

## 1. Gate Layer (2-of-5 Logic)
- Condition 1: close > 5d_avg_vwap
- Condition 2: volume > 20d_avg_volume × 1.2
- Condition 3: close >= 20d_high × 0.99
- Condition 4: 5d_stock_return > 5d_taiex_return
- Condition 5: Foreign/Trust net buy (2 of 3)

## 2. Pillar 3: Breakout Structure
- Breakout 20d/60d
- MA Alignment
- Relative Strength
- Upside Space
