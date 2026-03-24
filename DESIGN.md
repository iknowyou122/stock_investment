# Design System — 分點情報 API (Broker Label API)

## Product Context
- **What this is:** A B2B API that provides behavioral labels and confidence scores for Taiwan stock broker branches (分點). Developers query which brokers are accumulating, whether they're 隔日沖 (day-trade reversal) or 波段贏家 (swing winners), and the system's confidence in a signal.
- **Who it's for:** Taiwan quant/algo traders building their own tools, and CMoney/XQ plugin developers who want to power their signal products with broker behavioral data. Both personas land on the same landing page.
- **Space/industry:** Taiwan fintech / financial data API / quant developer tools
- **Project type:** Marketing landing page + API product signup (Phase 3b)
- **Language:** Bilingual — Traditional Chinese primary, English secondary

## Aesthetic Direction
- **Direction:** Intelligence / Forensic
- **Decoration level:** Intentional — subtle structure, no decorative blobs or gradients
- **Mood:** Bloomberg Terminal meets investigative journalism. Authoritative, data-forward, precise. This product uncovers hidden institutional behavior — it should feel like a forensics tool, not a generic SaaS dashboard. Every color and typographic choice reinforces this frame.
- **Key differentiation:** Every Taiwan financial data API uses green (profit signal) as their accent. This product sells behavioral intelligence, not price data. Amber/gold signals "pattern detected" not "price went up." This is a deliberate departure from the category.
- **Reference sites:** polygon.io (clean developer API), finnhub.io (dark trading theme), FinMind (Taiwan data baseline — functional, not designed)

## Typography
- **Display/Hero:** Instrument Serif (Google Fonts) — editorial weight, humanist. Sets this apart from every Inter/Roboto quant tool in Taiwan. The serif says "authoritative, The Economist" rather than "startup SaaS."
- **Body:** Geist (Google Fonts) — clean, technical, modern. Consistent with Vercel/developer ecosystem aesthetic.
- **UI/Labels:** Geist — same as body for labels, buttons, nav
- **Data/Tables/Code:** Geist Mono (Google Fonts) — tabular-nums for financial data, excellent for JSON responses and API examples
- **Loading:** Google Fonts CDN: `https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap`
- **Scale:**
  - 3xl: 52px (hero headline)
  - 2xl: 40px (section titles)
  - xl: 28px (sub-headlines)
  - lg: 20px (card titles, large labels)
  - md: 16px (body, descriptions)
  - sm: 14px (feature lists, pricing items)
  - xs: 12px (captions, labels, eyebrows)
  - xxs: 11px (monospace eyebrows, uppercase labels)

## Color
- **Approach:** Intentional dark — dark surface with two accent colors serving distinct semantic roles
- **Background:** `#0F1117` — deep charcoal (terminal, not blue-navy)
- **Surface:** `#1A1D23` — card and panel backgrounds
- **Surface 2:** `#21252D` — nested surfaces, code window bars
- **Text:** `#F0F4F8` — off-white primary text
- **Text Muted:** `#8B98A5` — secondary descriptions, metadata
- **Text Faint:** `#4A5568` — disabled states, decorative monospace labels
- **Accent (amber):** `#E8A100` — primary CTA, highlights, signal indicators. Deliberate break from green. Amber = "pattern detected," "signal found," "alert." Used for: CTA buttons, pricing card highlights, JSON numeric values, featured badge.
- **Data accent (teal):** `#00BFA5` — API response data, 波段贏家 label, live data indicators, checkmarks in pricing. Used to distinguish "good data" from "alert state."
- **Border:** `#262B33` — default borders
- **Border 2:** `#2F3740` — hover-state borders, slightly elevated surfaces
- **Semantic:** success `#28C840`, warning `#E8A100` (shares accent), error `#FC8181`, info `#7895D3`
- **Dark mode:** This is the primary/only mode for the product phase. A light mode exists as a toggle for accessibility and user preference.
- **Light mode overrides:** background `#F7F8FA`, surface `#FFFFFF`, text `#111318`, accent `#C47A00` (darkened amber for contrast), teal `#008F7A`

### CSS Custom Properties (implement as):
```css
:root {
  --bg:          #0F1117;
  --surface:     #1A1D23;
  --surface-2:   #21252D;
  --text:        #F0F4F8;
  --text-muted:  #8B98A5;
  --text-faint:  #4A5568;
  --accent:      #E8A100;
  --accent-dim:  rgba(232,161,0,0.12);
  --teal:        #00BFA5;
  --teal-dim:    rgba(0,191,165,0.12);
  --border:      #262B33;
  --border-2:    #2F3740;
}
```

### Label Badge Colors:
```css
--badge-daytrade:      rgba(232,161,0,0.18);   /* 隔日沖 — amber */
--badge-wave:          rgba(0,191,165,0.18);    /* 波段贏家 — teal */
--badge-geo:           rgba(120,149,211,0.18);  /* 地緣券商 — blue-slate */
--badge-managed:       rgba(180,120,211,0.18);  /* 代操官股 — purple */
```

## Spacing
- **Base unit:** 8px
- **Density:** Comfortable — not cramped (data-heavy products), not overly spacious (this isn't a luxury brand)
- **Scale:** 2xs(2) xs(4) sm(8) md(16) lg(24) xl(32) 2xl(48) 3xl(64) 4xl(80) 5xl(96)
- **Section vertical padding:** 96px top/bottom
- **Content max-width:** 1200px, 48px horizontal gutters
- **Card padding:** 28–36px (varies by card importance)

## Layout
- **Approach:** Grid-disciplined with editorial asymmetry in the hero
- **Hero:** Two-column: left copy (Instrument Serif headline + body + CTAs + stats), right code window (dark panel showing live API response with real Taiwan broker data)
- **Grid:** 12-column, 24px gaps. Cards typically 2-column or 3-column grids.
- **Max content width:** 1200px
- **Border radius:** sm(6px) md(8px) lg(10px) xl(12px) — no "bubbly uniform" radius on all elements
- **Code windows:** Always dark surface with macOS-style traffic lights, monospace font, amber/teal syntax highlighting
- **Section rhythm:** Hero → Label taxonomy → Data proof → API docs → Pricing → Signup CTA

## Motion
- **Approach:** Intentional — entrance animations on scroll, meaningful state transitions
- **Easing:** enter: ease-out, exit: ease-in, move: ease-in-out
- **Duration:** micro(50ms) short(150ms) medium(250ms) long(400ms)
- **Specific effects:**
  - Code window: amber glow pulse on the API response block (suggests "live data")
  - Section entrance: `opacity 0→1 + translateY 16px→0` over 300ms, staggered on cards
  - Button hover: `opacity 0.88` transition 150ms
  - Theme toggle: background/color transition 300ms
- **Never:** Auto-playing carousels, heavy parallax, animations that delay content

## Pricing Tiers (Phase 3b)
- **Free:** NT$0/month — 10,000 req/month — no credit card — primary acquisition CTA
- **Pro:** NT$500/month — 50,000 req/month — featured tier (amber border + accent-dim background)
- **Enterprise:** NT$2,000/month — 500,000 req/month — contact CTA

## Copywriting Direction
- **Chinese-first, then English.** Chinese headline first, English subtitle or label second.
- **No generic SaaS copy.** Do not use: "Unlock the power of," "All-in-one solution," "Seamlessly integrate."
- **Specific over evocative.** "隔日沖分點的 D+2 反轉率 74%" over "Superior data quality."
- **Show real broker names.** 凱基-台北, 元大-竹北, 摩根大通 — not "Branch A," "Branch B." Specificity is the trust signal.

## Decisions Log
| Date       | Decision | Rationale |
|------------|----------|-----------|
| 2026-03-24 | Amber/gold as primary accent instead of green | Every Taiwan financial data API uses green = profit. This product sells behavioral intelligence, not price data. Amber = pattern detected / alert. Deliberate category differentiation. |
| 2026-03-24 | Instrument Serif for display/hero | Every quant tool in Taiwan uses grotesque sans (Inter/Roboto). Serif says "authoritative investigative journalism" — matches the product's forensic framing and is unforgettable in the category. |
| 2026-03-24 | Real 分點 names in hero API response | No abstraction. Showing 凱基-台北 and 元大-竹北 in the hero code window creates instant "this is real Taiwan data" credibility for the target audience. |
| 2026-03-24 | Dark-first design | Standard for developer/data tools. The product's primary users (quant developers) are accustomed to dark IDEs and terminals. Light mode toggle provided for accessibility. |
| 2026-03-24 | Initial design system created | Created by /design-consultation based on competitive research (Polygon, Finnhub, FinMind) and three-layer synthesis of Taiwan fintech API landscape. |
