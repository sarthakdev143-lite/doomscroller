# Pricing: what we charge, what everyone else charges, and what it costs us

2026-11-27. The owner's launch prices, in rupees, published. Competitor prices and our costs come from
`docs/saas/costs-and-pricing.md` (the memo; its `[C..]`/`[S..]` source tags are kept). Dollar figures are converted
at **$1 = ₹92.67**: FRED series DEXINUS (Board of Governors H.10), observation for **2026-11-20**, the latest on
https://fred.stlouisfed.org/series/DEXINUS when read on 2026-11-27. The memo used ₹94; mid-November market reports
put the rupee at ₹93.5-94.1, so a ₹1-2 move changes every converted figure by about 1-2%.

## Headline

- **The cheapest competitor is WayinVideo Pro yearly at ₹0.89 per source minute** ($0.0096). That is a Black
  Friday price, 65% off its $26.99 list [C16].
- **Our most expensive minute, the ₹99 pack at ₹0.40/min, is 55% below it.** Our cheapest, Studio yearly at
  ₹0.21/min, is **76.5% below it**. Every monthly plan is 66-72% below it.
- **Against list prices** (no promo), the cheapest clippers are 2short.ai Premium monthly and Reap Creator yearly,
  both **₹1.54/min** [C7][C17]. We are **74-87% below** them.
- **The margin is the catch.** On today's zero-budget stack (free AI tiers, the owner's laptop), what we keep
  after Buy Me a Coffee fees is 63-90%. On the paid stack the memo recommends for scale, it is 14-37% at the
  3-clip job shape and -30% to +11% at 8 clips per 30 minutes (every plan negative). These prices are below the memo's own paid-stack cost
  floor of ₹0.67/min (the lowest price that clears 70%). **When the free tiers run out, prices or costs have to move.**

## Our prices

1 credit = 1 minute of source video. A job costs **max(ceil(source minutes), 3 × ceil(finished minutes))**. That is
the source length for normal jobs; the second term only applies when someone cuts many long clips from a short
video (section "The charge rule"). Packs never expire. Plans roll over at most one month of credits. Yearly = 10×
monthly (2 months free). API and MCP are on every paid plan; Studio has the priority queue.

| item | price | source minutes | ₹ / min | $ / min |
|---|---|---|---|---|
| Free tier | ₹0 | 10 minutes or 10 Shorts, whichever comes first, once per person | - | - |
| Pack 250 | ₹99 | 250 | **₹0.396** | $0.0043 |
| Pack 1,000 | ₹349 | 1,000 | ₹0.349 | $0.0038 |
| Pack 5,000 | ₹1,499 | 5,000 | ₹0.300 | $0.0032 |
| Creator monthly | ₹299 | 1,000 / mo | ₹0.299 | $0.0032 |
| Creator yearly | ₹2,990 | 12,000 / yr | ₹0.249 | $0.0027 |
| Pro monthly | ₹799 | 3,000 / mo | ₹0.266 | $0.0029 |
| Pro yearly | ₹7,990 | 36,000 / yr | ₹0.222 | $0.0024 |
| Studio monthly | ₹2,499 | 10,000 / mo | ₹0.250 | $0.0027 |
| Studio yearly | ₹24,990 | 120,000 / yr | **₹0.208** | $0.0022 |

## Competitors, per source minute

Effective price = plan price ÷ source minutes included, at full use (memo section 3). Klap is per clip, assuming
8 clips per 30-minute source. Submagic is per video, assuming 30-minute videos. Descript and Gling are editors that
meter "media minutes", listed for completeness. Riverside, VEED, CapCut and Captions publish no per-minute clipping
price [C11][C12][C13][C15].

| tool | plan | $ / min | ₹ / min | basis | source |
|---|---|---|---|---|---|
| WayinVideo | Pro yearly (Black Friday 65% off) | $0.0096 | **₹0.89** | annual, promo | [C16] |
| WayinVideo | Starter yearly (Black Friday) | $0.0112 | ₹1.03 | annual, promo | [C16] |
| WayinVideo | Scale yearly (Black Friday) | $0.0117 | ₹1.08 | annual, promo | [C16] |
| Descript (editor) | Creator yearly | $0.0133 | ₹1.24 | annual, media minutes | [C10] |
| 2short.ai | Premium monthly | $0.0166 | ₹1.54 | monthly | [C7] |
| Reap | Creator yearly | $0.0167 | ₹1.54 | annual | [C17] |
| 2short.ai | Pro monthly | $0.0221 | ₹2.05 | monthly | [C7] |
| Gling (editor) | Pro monthly | $0.0222 | ₹2.06 | monthly, second-hand | [C14] |
| Reap | Studio yearly | $0.0242 | ₹2.24 | annual | [C17] |
| Vizard | Creator yearly | $0.0242 | ₹2.24 | annual, second-hand | [C2] |
| WayinVideo | Pro monthly (list) | $0.0270 | ₹2.50 | monthly | [C16] |
| WayinVideo | Starter monthly (list) | $0.0320 | ₹2.96 | monthly | [C16] |
| 2short.ai | Lite monthly | $0.0330 | ₹3.06 | monthly | [C7] |
| Klap | Basic yearly | $0.0390 | ₹3.61 | annual, per clip | [C3] |
| Reap | Creator monthly | $0.0400 | ₹3.71 | monthly | [C17] |
| Descript (editor) | Hobbyist monthly | $0.0400 | ₹3.71 | monthly, media minutes | [C10] |
| Quso | Growth yearly | $0.0408 | ₹3.78 | annual | [C6] |
| Submagic | Starter yearly (₹666/mo, 5 videos) | - | ₹4.44 | annual, priced in INR | [C4] |
| Spikes Studio | PRO+ yearly | $0.0470 | ₹4.35 | annual | [C8] |
| Vizard | Creator monthly | $0.0483 | ₹4.48 | monthly, second-hand | [C2] |
| Opus Clip | Pro yearly | $0.0483 | ₹4.48 | annual | [C1] |
| Submagic | Starter monthly (₹799, 5 videos) | - | ₹5.33 | monthly, priced in INR | [C4] |
| Munch | Premium | $0.0600 | ₹5.56 | same monthly or yearly | [C5] |
| Choppity | Creator yearly | $0.0747 | ₹6.92 | annual | [C9] |
| Munch | Essential | $0.0760 | ₹7.04 | same monthly or yearly | [C5] |
| Klap | Basic monthly | $0.0770 | ₹7.14 | monthly, per clip | [C3] |
| Quso | Growth monthly | $0.0817 | ₹7.57 | monthly | [C6] |
| Opus Clip | Pro monthly | $0.0967 | ₹8.96 | monthly | [C1] |
| Opus Clip | Starter monthly | $0.1000 | ₹9.27 | monthly | [C1] |
| Choppity | Creator monthly | $0.1067 | ₹9.88 | monthly | [C9] |
| Spikes Studio | PRO+ monthly | $0.1100 | ₹10.19 | monthly | [C8] |

**How far below the cheapest competitor we are** (WayinVideo promo ₹0.888/min; non-promo floor ₹1.541/min):

| our item | ₹ / min | below WayinVideo promo | below the list-price floor |
|---|---|---|---|
| Pack 250 | 0.396 | 55.4% | 74.3% |
| Pack 1,000 | 0.349 | 60.7% | 77.4% |
| Pack 5,000 | 0.300 | 66.2% | 80.6% |
| Creator monthly / yearly | 0.299 / 0.249 | 66.3% / 71.9% | 80.6% / 83.8% |
| Pro monthly / yearly | 0.266 / 0.222 | 70.0% / 75.0% | 82.7% / 85.6% |
| Studio monthly / yearly | 0.250 / 0.208 | 71.9% / 76.5% | 83.8% / 86.5% |

The market leader, Opus Clip, is ₹4.48 (Pro yearly) to ₹9.27 (Starter monthly) per minute: 11-45x our price.

**Caveats that apply to any "cheapest" claim:**
- **WayinVideo's floor is a sale.** At list it is ₹2.50 (Pro monthly).
- **Vizard's upper credit tiers are unpriced on its own page** [C2]. Check them by hand before printing "cheapest on
  the internet".
- **Units differ.** Credits mean source minutes for us, WayinVideo, Reap and Opus; clips for Klap; videos for
  Submagic.

## The charge rule

A finished minute costs us about 16× a source minute (memo §4.3), so the rule charges
**max(ceil(source minutes), 3 × ceil(finished minutes))**. The job reserves the most it can cost: the source, or 3 ×
clips × the longest clip the options allow, whichever is larger. The estimate (`POST /api/jobs/estimate`, also
`/api/v1/estimate` and the MCP `estimate_video` tool) returns that number as `credits`, with `basis: "source" |
"finished"`. The job is then settled on the real finished length, never above what was reserved.

**How it lands:**
- **Normal jobs pay source minutes.** A 23-minute talk cut into 3 × 25 s Shorts costs max(23, 3) = 23. Thirty
  minutes into 8 × 30 s costs max(30, 12) = 30.
- **A short source cut into many long clips pays for the output.** A 5-minute video cut into 10 × 60 s Shorts costs
  max(5, 30) = 30.
- **At the worst case (3× the source), our prices still undercut the WayinVideo promo, except the smallest pack.**
  Studio yearly becomes ₹0.625 per source minute, still 30% below WayinVideo's promo. The ₹99 pack becomes ₹1.19,
  above the promo but still below every list price.

## What it costs us, and the margin per item

Costs come from the memo's per-credit model (stack (c): Groq pick pass + Deepgram on caption windows + Claude Haiku
5.5 + Hetzner, memo §4.1-4.2):
- **3-clip shape E:** $0.0017 = **₹0.158 per credit**.
- **8-clips-per-30-minutes shape H:** $0.0027 = ₹0.250.
- **Worst case under the charge rule:** $0.0036 = ₹0.334.

The zero-budget stack the owner launches on (memo §9.4-9.5) runs on Groq's free tier and the owner's laptop. Its
cash cost is **₹0 per credit**, but capacity is capped at ~19 free signups a day by Groq's free token limits. That
budget is shared with paying users.

Buy Me a Coffee fees (memo §9.3) are 5% platform + Stripe 2.9% + $0.30 (₹27.80) + 1% non-US card + 0.5% payout,
plus 0.5% more on memberships. The fixed ₹27.80 dominates small payments.

Margin = (price - BMC fee - cost of every credit used) ÷ price, **at 100% of credits used** (unused credits only
raise it):

| item | price | BMC fee | margin, free stack | margin, paid stack shape E | margin, paid stack shape H |
|---|---|---|---|---|---|
| Pack 250 | ₹99 | ₹37.11 (37.5%) | 63% | 23% | -1% |
| Pack 1,000 | ₹349 | ₹60.61 (17.4%) | 83% | 37% | 11% |
| Pack 5,000 | ₹1,499 | ₹168.71 (11.3%) | 89% | 36% | 5% |
| Creator monthly | ₹299 | ₹57.40 (19.2%) | 81% | 28% | -3% |
| Creator yearly | ₹2,990 | ₹323.81 (10.8%) | 89% | 26% | -11% |
| Pro monthly | ₹799 | ₹106.90 (13.4%) | 87% | 27% | -7% |
| Pro yearly | ₹7,990 | ₹818.81 (10.2%) | 90% | 19% | -23% |
| Studio monthly | ₹2,499 | ₹275.20 (11.0%) | 89% | 26% | -11% |
| Studio yearly | ₹24,990 | ₹2,501.81 (10.0%) | 90% | 14% | -30% |

**What the table means:**
- **On the free stack every item is profitable**, but a plan can't be used fully at scale. Groq's free Whisper
  allowance is 480 audio-minutes a day (~14,400 a month, memo §9.5), shared by everyone. One fully used Studio
  plan (10,000 minutes) would take most of it.
- **On the paid stack the prices are below cost for denser jobs.** The memo's floor for 70% margin is $0.0072 per
  credit (₹0.67/min) at shape E. Our prices are about a third of that. When revenue moves us to paid infrastructure
  (memo §9.5 spending order), either raise prices toward that floor or cut the cost per credit. At the ₹0.67/min
  floor we would still be 25% below the WayinVideo promo and 57% below the list-price floor (₹1.54).
- **The ₹99 pack loses 37.5% to BMC's fixed fee.** A larger minimum pack, or a UPI channel without the $0.30, would
  recover most of it.
- **BMC prices in USD by default** (memo §10.4). If the BMC page charges dollars, record the exact charge on each
  item (`scripts/saas_manage.py bmc-map ... --bmc-amount 1.07 --bmc-currency USD`). Webhooks are verified against it.

## Sources

- Exchange rate: FRED, "Indian Rupees to U.S. Dollar Spot Exchange Rate" (DEXINUS), 92.67 on 2026-11-20 —
  https://fred.stlouisfed.org/series/DEXINUS (read 2026-11-27).
- Costs, fees and capacity: `docs/saas/costs-and-pricing.md` §4.1-4.3 (stack and per-credit costs), §9.3 (Buy Me a
  Coffee fees [S50]), §9.4-9.5 (free tiers and capacity), §10.4 (BMC currency [S74]).
- Competitors (memo source list, seen 2026-11-27 unless noted):
  [C1] Opus Clip https://www.opus.pro/pricing ·
  [C2] Vizard https://vizard.ai/pricing (prices second-hand: https://contenttoolslab.com/reviews/vizard-ai-pricing/) ·
  [C3] Klap https://klap.app/pricing · [C4] Submagic https://www.submagic.co/pricing ·
  [C5] Munch https://www.munchstudio.com/pricing · [C6] Quso https://quso.ai/pricing ·
  [C7] 2short.ai https://www.2short.ai/pricing · [C8] Spikes Studio https://spikes.studio/pricing ·
  [C9] Choppity https://www.choppity.com/pricing · [C10] Descript https://www.descript.com/pricing ·
  [C11] Riverside https://riverside.com/pricing · [C12] VEED (second-hand) https://www.veed.io/pricing/plans.md ·
  [C13] CapCut (second-hand) https://www.capcut.com/help/how-much-does-capcut-pro-cost ·
  [C14] Gling (second-hand) https://www.xpay.sh/saas-pricing/gling-ai/ ·
  [C15] Captions (second-hand) https://www.hooked.so/compare/captions-ai-pricing ·
  [C16] WayinVideo https://wayin.ai/pricing/ (rendered 2026-11-27, Black Friday prices) ·
  [C17] Reap https://reap.video/pricing
