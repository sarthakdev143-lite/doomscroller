# Costs and pricing: what one job costs, and what to charge

Research memo, 2026-11-27. Every vendor or competitor number carries a source tag (`[S..]`, `[C..]`); the list
at the end gives the URL and the date it was seen. "Second-hand" means the number came from a third-party page,
not the vendor's own. FX used throughout: **$1 = ₹94** (Nov 2026 quotes cluster at ₹93.5-94.5 [S40]) and
**€1 = $1.10** [S40].

> **Provisional.** The price table below assumes paid infrastructure (Hetzner, Haiku 5.5 API) and Dodo/Razorpay
> fees. The owner has since chosen a **zero-budget launch** (section 9):
> - payments through Buy Me a Coffee memberships, which cost about 3 margin points more than Dodo;
> - no registered company;
> - Cloudflare's free tier;
> - the owner's laptop as the render worker;
> - a free tier of 10 shorts or 10 source minutes, whichever comes first.
>
> Keep the prices as list prices, and re-price once the first 100 paid jobs have been metered.

## 0. Decisions in one page

**Unit.** 1 credit = 1 minute of source video, rounded up per job. A job is charged
`max(source minutes, 3 x finished minutes)`. Normal jobs never hit the second term (finished clips are under a
third of the source); it exists because a finished minute costs us about 16x a source minute (section 4.3).

**Price table (USD, via Dodo Payments; India in INR via Razorpay, GST included)**

| | credits | monthly | annual (2 months free) | $/credit monthly / annual | gross margin* monthly / annual |
|---|---|---|---|---|---|
| Free | 60 once per verified identity | $0 | - | - | cost $0.09 per signup |
| Pack S (never expires) | 250 | $4 one-time | - | $0.016 | 74% |
| Pack M | 1,000 | $12 one-time | - | $0.012 | 77% |
| Pack L | 5,000 | $49 one-time | - | $0.0098 | 76% |
| **Creator** | 1,000 / mo | **$10** | **$100/yr ($8.33/mo)** | $0.010 / **$0.0083** | 73% / 73% |
| **Pro** | 3,000 / mo | **$27** | **$270/yr ($22.50/mo)** | $0.0090 / $0.0075 | 74% / 71% |
| **Studio** | 10,000 / mo | **$89** | **$890/yr ($74.17/mo)** | $0.0089 / $0.0074 | 74% / 71% |
| Pro BYOK (API/MCP developers) | 3,000 / mo | $13 | $130/yr | $0.0043 | 87% |
| India Lite | 300 / mo | ₹299 | ₹2,990/yr | ₹1.00 | 78% / 75% |
| India Creator | 1,000 / mo | ₹849 | ₹8,490/yr | ₹0.85 | 75% / 71% |
| India Pro | 3,000 / mo | ₹2,499 | ₹24,990/yr | ₹0.83 | 75% / 70% |
| India Studio | 10,000 / mo | ₹8,399 | ₹83,990/yr | ₹0.84 | 75% / 70% |

\* At 100% of credits used and the **owner's 3-clip shape** (23-min source, 3 clips: what the live system does
today, because every job asked for 3). Unused credits only raise these. Margin depends on **clip density**, not
on source minutes. The 70% line holds up to **4-5 clips per 30 source minutes** (1 per 6-7 min). At 8 clips per
30 min it is **57-63% in USD and 54-67% in INR** (section 5.9).

**Where cheapest and 70% conflict** (jobs denser than ~5 clips per 30 min), **cheapest wins** and margin falls
to 57-63%, still about 2.5x variable cost. The fix is cost, not price. The Deepgram caption windows are 47% of a
clip's cost. If Groq's own word timing passes the pre-launch caption check (section 7, risk 5), drop the windows,
and 70% then holds up to **8-10 clips per 30 min** (69.5-73% at 8 clips).

**Cheapest claim.** Creator annual at **$0.0083 per source minute** is 13% below the cheapest
feature-equivalent competitor, WayinVideo Pro yearly at $0.0096. That figure is verified on WayinVideo's own page
today: 1 credit = 1 source minute, 12,000 credits a year, B-roll included [C16]. It is a **Black Friday 65%-off
price** (list $26.99). If it reverts, the next floor is Reap annual at $0.0167 [C17], and we would be 50% below.
Every monthly plan, at $0.0089-0.010, is at least 40% below the cheapest monthly competitor (2short Premium,
$0.0166 [C7]).

The lowest price that still clears 70% depends on the job shape: **$0.0072 per credit** at the 3-clip shape,
$0.0114 at 8 clips per 30 min.

**Cost of one video.** A 30-minute English podcast cut into 8 thirty-second clips with B-roll costs us
**$0.082**: STT $0.046 (Groq full pass $0.020 + Deepgram on the 8 clip windows $0.026), LLM $0.019 (Claude Haiku
5.5, 43 calls, ~80k input / 19k output tokens), render $0.011 (128 s on a Hetzner AX42), storage $0.001 (88 MB on
R2 for 30 days), plus 6% for failed jobs. It uses 30 credits, which sell for $0.25 (Creator annual) to $0.48
(smallest pack). This assumes an upload or a link the server can download without a proxy (risk 1).

The same video through today's pipeline at API prices would cost **$0.36**: Deepgram full pass plus Claude Haiku
4.5, its configured primary model. That is more than Creator annual charges for it.

**Stack.**
- **Pick pass:** Groq `whisper-large-v3-turbo` transcribes every source minute to pick moments and detect
  language. Its Developer plan caps Whisper at 8 audio hours a day (~14,400 source minutes a month) [S41].
  Overflow goes to self-hosted faster-whisper on the server's idle threads, at about half Groq's cost but slower.
- **Caption pass:** only the chosen clip windows are re-transcribed, by Deepgram Nova-3 (English) or Sarvam
  (Hindi).
- **AI:** Claude Haiku 5.5 runs every call through the API.
- **Everything else:** one Hetzner AX42 for rendering, R2 for storage, Dodo Payments (global) plus Razorpay
  (India), Resend for email, Turnstile for bot checks.
- **Link imports at launch:** downloaded through the owner's home connection, the one IP proven to work; a
  Hetzner IP has not been tested against YouTube yet.

Fixed costs are **$155/month**, and break-even is **21 Creator-equivalent subscribers**.

**The whole price table is conditional on one untested change:** the Groq pick pass (section 7, risk 5). If it
fails the quality check, the fallback stacks raise cost per credit and the table needs re-pricing before launch.

**What this changes elsewhere** is listed in section 8. The contract changes are: Dodo replaces Stripe, free
credits go from 30 to 60, API/MCP opens to every paid plan, and the 3x finished-minute rule is added.

---

## 1. Measured reality (owner's live system, read-only)

Sources: `doomscroller.db` (`clip_jobs`, `videos`) opened read-only, and `logs/doomscroller.log{,.1,.3}`
covering 2026-11-04 to 2026-11-27. The 36 source files kept in `content/long/` were probed with ffprobe, and one
ffmpeg benchmark was run on this machine.

**Machine:** Intel Core i5-3320M @ 2.60 GHz, 2 cores / 4 threads (2012 laptop), 8 GB RAM, PassMark 2,655 [S20].
It runs the server at below-normal priority beside the API, growth loop and tunnel, so wall-clock numbers here
are pessimistic.

### Jobs

| measure | value |
|---|---|
| clip jobs | 103: 97 done, 6 failed = **5.8%** |
| failure causes | ffmpeg timeout at 3,600 s (2), stopped 3 times (1), DNS failure on download (1), video removed (1), no usable moments (1) |
| jobs needing a 2nd+ attempt | 29 of 103, mostly laptop sleep/restarts (`started_at` resets per attempt) |
| clips requested per job | 3 on every job |
| done jobs that delivered **fewer clips than asked** | **20 of 97 (21%)**; mean 2.68 clips per job |
| source length (36 kept sources) | median **22.8 min**, mean 26.8, range 8.3-97.6 |
| source download size | 720p H.264 (downloader caps at 720p), median **12.6 MB per source minute** |
| speech density | 181 words per minute (Deepgram and Sarvam transcripts) |
| output clip length | median 24.9 s (English 27.8 s, Hindi 36.5 s); **11.0 MB per clip file** |
| language mix | Hindi via Sarvam: 32 runs; English via Deepgram: 12 runs (46 runs matched to logs) |

### Where the time goes (medians, this laptop)

| stage | English | Hindi | what it is |
|---|---|---|---|
| download + audio extract + STT | 230 s (n=12) | 202 s (n=32) | network bound |
| caption check + signals + moment pick + payoff check | 327 s (n=7) | 276 s (n=31) | mostly waiting on the AI CLI |
| per clip: edit pass + B-roll judge + render + audio QA | 244 s (n=7) | 105 s (n=31) | render ~120 s of it; Hindi skips edit/B-roll |
| **whole job, ~3 clips** | **1,505 s (n=12)** | **1,124 s (n=32)** | |

The middle rows have smaller n because five English runs logged no moment-extension line to time against.

Render cost, isolated:
- **Synthetic benchmark:** framed layout (blurred backdrop + centred 720p source), libx264 `veryfast` CRF 20,
  1080x1920 @ 30 fps, AAC. **87.7 s wall for a 24 s clip.**
- **Real renders** (captions, zooms, SFX mix, overlays) are timed from the last clip's final log line to job end.
  Median **123 s** (n=19), which includes metadata and audio QA. The real graph therefore costs about 1.3x the
  synthetic one, roughly **5 laptop-seconds per output second**.
- **On a server.** An AMD Ryzen 7 PRO 8700GE (Hetzner AX42) scores PassMark 28,155 [S21], **10.6x** this laptop,
  so a 25-30 s clip takes ~12 s of the whole box. This memo budgets **15 box-seconds per 30 s clip** and
  **0.25 box-seconds per source minute** for decode, signals and audio extract (derived, not measured on a server).

Most wall time is waiting, not CPU. The AI CLIs (OpenCode, then Claude CLI) are slow and flaky:
- OpenCode failed over to Claude 19 times.
- **8 of 12 English runs lost their B-roll because the AI could not judge stock shots** (14 runs overall).

An API-called model removes both the wait and the loss. A SaaS cannot run on CLI subscriptions anyway, so every
AI cost below is at API rates.

### AI calls per job (read from the code)

Speech path, N clips, B-roll on. Titles, descriptions and hashtags come back inside call 2. The per-clip
`generate_metadata_for_video` reuses that title ("platform" path) and makes no extra call.

| # | call | where | input | output |
|---|---|---|---|---|
| 1 | burned-in caption check | `subtitles.py` | 8 frames 480x270 (~180 image tokens each) + ~200 | ~30 |
| 2 | pick moments (titles, hooks, descriptions, tags) | `moments.py` `_speech_prompt` | **whole transcript** as numbered sentences + ~1,200 instructions | ~180 per proposal, 2N proposals |
| 3 | payoff check | `moments.py` `_payoff_prompt` | 2N proposals x ~375 | ~15 per proposal |
| 4 | edit pass, per clip | `edit.py` `_edit_prompt` | ~800 | ~400 |
| 5 | B-roll judge, per cutaway | `shots.py` | one ~1,100x970 contact sheet (1,400 image tokens) + ~300 | ~100 |

- **B-roll judge calls:** 2.2 per clip on English runs where judging worked (3 runs, 9 clips: a small sample).
  The cost model budgets 4 per clip.
- **Retries:** `ask_json` retries once on a non-JSON reply, so the model adds 10% to LLM cost.
- **Visual path:** the no-speech path replaces calls 2-5 with one vision call of 2N frame strips.
- **Image tokens:** Claude counts `ceil(w/28) x ceil(h/28)` visual tokens per image, and none of these images is
  large enough to be downscaled [S10].
- **Text tokens:** English is estimated at ~1.7 tokens per word, derived from ~1.3 on the old tokenizer plus the
  "approximately 30% more tokens" of Claude 4.7+ [S9]. Devanagari Hindi is ~3.5.

**Hindi is transcribed twice today.** `transcribe.py` always calls Deepgram first and only calls Sarvam when
Deepgram detects Hindi, so every Hindi minute pays both: $0.0095 per minute at the prices below.

---

## 2. Vendor prices (verified)

### 2.1 Speech-to-text (per source minute; 30-min cost in brackets)

| vendor / model | price | 30 min | word timestamps | source |
|---|---|---|---|---|
| **Groq whisper-large-v3-turbo** | $0.04 / audio hour = **$0.00067/min** (10 s minimum per request) | $0.020 | **yes** (`verbose_json` + `timestamp_granularities=word`) | [S2] |
| Groq whisper-large-v3 | $0.111 / h | $0.056 | yes | [S2] |
| **Deepgram Nova-3 pre-recorded** | **$0.0043/min** pay-as-you-go mono; $0.0052 multilingual; Growth $0.0036 / $0.0043; smart formatting included; $200 free credit | $0.129 | yes (the live pipeline uses them) | [S1] |
| AssemblyAI STT-3.7 / Universal-2 | $0.21 / h; $0.15 / h; $50 free | $0.105 / $0.075 | not confirmed on the pricing page | [S3] |
| ElevenLabs Scribe v3/v2 batch | $0.20 / h | $0.100 | not confirmed on the pricing page | [S4] |
| OpenAI gpt-4o-mini-transcribe | ~$0.003/min (token-priced) | $0.090 | **no**: only `whisper-1` ($0.006/min) takes timestamp granularities, and it is deprecated for that use from 2027-02-26 | [S5][S6] |
| **Sarvam STT batch** | **₹0.49/min** (= $0.0052); ₹0.59 with diarization; ₹100 free | $0.156 | chunk-level only (see CLAUDE.md) | [S7] |
| self-hosted faster-whisper, CPU | int8 on 8 threads of an i7-12700K: 13 min of audio in 1 m 42 s, **~7.6x realtime** (model not named in the README) | box time only | yes | [S8] |

Groq at $0.04/h is 6.5x cheaper than Deepgram and 7.8x cheaper than Sarvam. A GPU box does not pay off: a
Hetzner GEX44 at about €184/mo [S17] equals Groq turbo only above ~5,000 audio hours a month (derived).

**Groq's cap.** On the Developer plan both Whisper models are limited to 20 requests/min, 2,000 requests/day,
7,200 audio-seconds/hour and **28,800 audio-seconds/day** [S41]. That is ~14,400 source minutes a month, about
14 fully used Creator plans.

**Plan:**
- Ask Groq for a higher limit before launch.
- Meanwhile, route overflow to **self-hosted faster-whisper (int8) on the AX42's idle threads.** At ~7.6x realtime
  on 8 threads [S8], that costs ~4 box-seconds per source minute, **$0.00033/min at 50% load: half Groq's price.**
  The catch: it takes ~4 minutes of wall time per 30-min source and cuts one box's capacity to roughly 220k source
  minutes a month.

### 2.2 LLM (per million tokens; cost of the 30-min / 8-clip job in brackets)

The job uses ~80k input and 19k output tokens (output doubled to allow for thinking), plus 10% for retries.

| model | input / output | job cost | source |
|---|---|---|---|
| **Claude Haiku 5.5** (prompts up to 100k tokens) | **$0.10 / $0.50**; batch $0.05 / $0.25; over 100k: $0.50 / $2.50 | **$0.019** | [S9] |
| Claude Haiku 4.5 (the owner's current OpenCode primary) | $1 / $5 | $0.194 | [S9] |
| Claude Sonnet 5.5 (the Claude CLI fallback's "sonnet") | $2 / $10 | $0.388 | [S9] |
| Gemini 2.5 Flash-Lite | $0.10 / $0.40 | $0.017 | [S11] |
| Gemini 3.1 Flash-Lite | $0.25 / $1.50 | $0.054 | [S11] |
| Gemini 3.8 Flash | $0.75 / $3.75 until 2026-12-31, then $1.50 / $7.50 | $0.145 | [S11] |
| OpenAI gpt-5-nano / gpt-5-mini | $0.05 / $0.40; $0.25 / $2.00 | $0.013 / $0.064 | [S5] |
| DeepSeek deepseek-flash (vision) | $0.30 / $1.20 peak, half off-peak | $0.052 / $0.026 | [S12] |
| OpenRouter | provider price passed through; 5.5% fee on credit purchases ($0.80 minimum) | +5.5% | [S13] second-hand |

**Pick: Claude Haiku 5.5.** The cheap models all land within $0.01 per job of each other, so quality decides:
- It is the next generation of the model the pipeline already uses as its primary.
- It takes images and structured output.
- It avoids a cross-vendor prompt port.
- The bill moves by less than $0.001 per credit either way.

**Two guards:**
- Keep each prompt under 100k tokens, the point where Haiku 5.5's price jumps 5x. In the moments call, English
  reaches 100k at about 5.4 hours of source and Devanagari Hindi at about 2.6 hours, so chunk the transcript at
  90k tokens.
- Run the judge and edit calls at effort `low`.

### 2.3 Compute

| option | spec | price | source |
|---|---|---|---|
| **Hetzner AX42** | Ryzen 7 PRO 8700GE, 8 cores / 16 threads (Zen 4), 64 GB DDR5 | **€99/mo + €49 setup ($108.90/mo)** | [S14] |
| Hetzner AX102 | Ryzen 9 7950X3D, 16 cores | €259/mo + €129 setup | [S14] |
| Hetzner CCX33 (cloud, dedicated vCPU) | 8 vCPU, 32 GB, 30 TB traffic | €117.99/mo cap, €0.1891/h | [S15] |
| Hetzner CPX42 (cloud, shared) | 8 vCPU, 16 GB | €59.49/mo (HEL1) | [S16] |
| AWS c7a.2xlarge, us-east-1 | 8 vCPU | $0.4106/h on demand (~$300/mo); spot ~$0.146-0.157/h with >20% interruptions | [S18] second-hand |
| Contabo VPS 30 | 8 vCPU, 24 GB (shared, oversold) | ~€14/mo | [S19] second-hand |
| OVHcloud Advance-2 2026 | AMD EPYC 4345P, 8 cores / 16 threads | €134.99/mo excl. VAT (Rise line from €54.99, spec unconfirmed) | [S42] second-hand |
| Hetzner GEX44 / GEX45 (GPU) | RTX 4000 SFF Ada / RTX PRO 4000 Blackwell | €184/mo at launch (2026 price adjustment lists €232.30 excl. IPv4) / €214/mo + €209 setup | [S17] second-hand |

**Pick: one AX42.** It has 16 threads of Zen 4 for less than an 8-vCPU cloud box. At 50% average load it
supplies 1.3M box-seconds a month, enough for **~690k source minutes (measured shape) or ~305k (8-clip shape)**.
Add a second AX42 when queue wait p90 passes 10 minutes. NVENC is not needed: at $0.0013 of CPU per clip, a GPU
saves nothing.

### 2.4 Storage, email, bot protection

| need | pick | price | source |
|---|---|---|---|
| clip storage and delivery | **Cloudflare R2** | $0.015/GB-month, **free egress**, 10 GB/month free | [S22] |
| (alternative) | Backblaze B2 | $6.95/TB-month, free egress up to 3x stored, then $0.01/GB | [S23] |
| email | **Resend** now; Amazon SES past 100/day | Resend free: 3,000/mo, 100/day; Pro $20 for 50k. SES $0.10/1,000. ZeptoMail $2.50/10,000 (second-hand) | [S24][S25][S26] |
| bot protection | **Cloudflare Turnstile** | **free**, unlimited challenges, 20 widgets, 10 hostnames each | [S27] |

R2 wins over B2 because customers stream and download every clip, and R2 egress is free with no ratio. A clip
kept 30 days costs $0.00017 (11 MB).

### 2.5 Payments (solo founder in India, selling worldwide)

| provider | fee | status for an Indian founder | source |
|---|---|---|---|
| **Dodo Payments** (merchant of record) | 4% + 40¢ on US cards; **+1.5% non-US cards**; **+0.5% subscriptions / usage billing**; India INR 4% + 15¢; payouts listed free in the table, but the FAQ says $5 under $1,000 and $25 SWIFT for non-US businesses (fixed costs below assume $25 a month) | markets to Indian sellers; payouts to an Indian bank and Aadhaar/PAN KYC are vendor-stated, not independently confirmed | [S33] |
| Paddle (merchant of record) | 5% + 50¢, all-inclusive | India is a supported *buyer* country with UPI Autopay; *seller* onboarding from India is not confirmed by Paddle; INR is not a payout currency (second-hand) | [S31] |
| Stripe | ~3-4.3% (second-hand) | **invite-only in India**; no self-serve signup | [S28] |
| Stripe Managed Payments | 3.5% + processing + FX | **not open to India-based sellers** (second-hand; GA 2026-04-22) | [S29] |
| Lemon Squeezy | 5% + 50¢ | **no new signups; shuts down 2027-03-31** | [S32] |
| **Razorpay** (India, INR) | **2%** domestic cards/UPI/netbanking; international up to 3%; card subscriptions 0.9% + platform fee; **18% GST on fees** | works | [S30] |

**Pick: Dodo for everyone paying in USD, Razorpay for INR checkout in India.** Dodo, as merchant of record,
files VAT/GST/sales tax in every buyer country; that is the only way a one-person company sells to 200+
countries. Its effective rate on a typical non-US subscription is **6% + $0.40**. That fixed 40¢ sets the
**minimum ticket at $4** (fee 16%). Paddle is the backup if Dodo's payout terms disappoint.

**This contradicts the contract,** which hardcodes `/webhooks/stripe`. The core agent should implement
`/webhooks/dodo` instead; Stripe is not available to this founder without an invite. Confirm the GST treatment
of Dodo payouts (export of services to Dodo vs. OIDAR) with a CA.

### 2.6 Content APIs and licensing

| source | terms | verdict | source |
|---|---|---|---|
| **Pexels** | free; **200 requests/hour and 20,000/month by default**, unlimited free on request; "show a prominent link to Pexels", credit photographers; may not replicate Pexels' core function | OK for a SaaS editor. **Apply for unlimited before launch**: at ~4 searches per clip, 200/h caps us at ~6 eight-clip jobs an hour | [S34] |
| **Pixabay** | requests **must be cached 24 h**; 100 requests/60 s per key (archived 2022 docs); key in the URL; no indefinite hotlinking | OK; make it the primary B-roll source and share the 24 h cache across customers | [S35] |
| **Tenor** | **API shut down 2026-06-30**; no new keys since 2026-01-13 | dead: remove the `tenor_api_key` path | [S36] |
| **Giphy** | production use needs an approved production key with **custom (unpublished) pricing** and "Powered by GIPHY" attribution | GIFs are clips of films, TV and streams; burning them into a customer's monetised Short is third-party copyright exposure | [S37] |
| MemeGen templates | free images of copyrighted meme templates | same exposure as Giphy | |
| **MyInstants** sounds (`myinstants.py`, `trending_sfx.py`) | no licence found; user uploads of TV, film, stream and song audio | **unlicensed**; Content ID matches audio. Off for customers | (search found no terms) |
| **Freesound** | API commercial use is "negotiated case by case"; may not sublicense the API to third parties | use a fixed, curated CC0 pack shipped as our own files; do not call the API per customer | [S38] |
| music | owner-chosen tracks; e.g. a CC BY 4.0 track that needs a description credit | ship CC0 or owned tracks only; keep the automatic credit line for CC BY | |

**Defaults for customers:** B-roll on (Pexels + Pixabay), SFX on (synthesised + CC0 pack), music on (CC0/owned
library), **memes off and meme sounds off.** A customer can still switch memes on, behind a one-line warning that
these are third-party clips and may draw claims.

---

## 3. Competitors (2026 pricing)

**Effective $/source minute** = plan price ÷ source minutes included, at full use. For per-clip plans (Klap) it
assumes 8 clips per 30-min source; for per-video plans (Submagic) it assumes a 30-min video.

**Feature-equivalent** = AI clipping with word captions, no watermark, and B-roll on that plan.

| tool | monthly | annual (/mo) | included | $/min monthly / annual | free tier (watermark) | max source | B-roll | auto-post | API | MCP | src |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **WayinVideo** | list: Starter $15.99, Pro $26.99, Scale $139.99 | Black Friday 65% off: Starter **$5.58**, Pro **$9.58**, Scale $69.99 | 6,000 / 12,000 / 72,000 credits/yr; "1 credit processes 1 min of source video" | list $0.032 / $0.027; **promo $0.011 / $0.0096** / $0.0117 | 100 one-time credits, 720p, 3-day export; no watermark (help centre) | 10 / 30 GB; Pro reframe 120 min | Pro ("AI image/video B-roll") | Starter+ (3 / 10 / 50 accounts) | Enterprise only | not stated | [C16] own page, 2026-11-27 |
| **Reap** | Creator $24 / Studio $50 | $9.99 / $29 | 600 / 1,200 min | $0.040 / $0.0167; $0.042 / $0.024 | 60 min/mo, 720p, watermark | 10 GB upload | listed, plan unclear | yes | yes | **yes, all paid** | [C17] |
| **2short.ai** | Lite $9.90 / Pro $19.90 / Premium $49.90 | not stated | 5 h / 15 h / 50 h | $0.033 / $0.022 / **$0.0166** | 30 min | not stated | not mentioned | not mentioned | not mentioned | not mentioned | [C7] |
| **Vizard** | Creator $29 / Business $39 (second-hand) | $14.50 / $19.50 | 600+ credits/mo (1 credit = 1 min) | $0.048 / $0.024 | 60 credits/mo, 720p, 10-min export, watermark | 10 h (free: 60 min) | all plans | all plans | **all plans** | **all plans incl. free** | [C2] |
| **Opus Clip** | Starter $15 / Pro $29 | Pro $14.50 | 150 / 300 credits (1 = 1 min) | $0.100 / $0.048 | 60 credits/mo, watermark, 3-day expiry | 10 GB / 30 GB | Starter 3 clips/mo; Pro unlimited stock | Starter+ | Pro | **Pro** | [C1] |
| **Klap** | Basic $29 / Pro $79 / Pro+ $189 | $14.50 / $39.50 / $94.50 | 100 / 300 / 1,000 clips | $0.077 / $0.039 (Basic) | none (1-video trial) | 3 h | yes | yes | **$0.44/input video + $0.32/short + $0.48/export** | not mentioned | [C3] |
| **Quso** (ex vidyo.ai) | Lite $29 / Essential $39 / Growth $49 | $14.50 / $19.50 / $24.50 | 100 / 300 / 600 credits | Growth $0.082 / $0.041 | 75 credits, 720p, watermark | not stated | library, plan not tied | Essential+ | not stated | **all plans incl. free** | [C6] |
| **Spikes Studio** | PRO+ $32.99 / Ent $115.99 | $14.09 / $56.34 | 300 / 1,200 min | $0.110 / $0.047 | 30 min/mo, watermark | 5-min web editor | yes | annual | on application | not mentioned | [C8] |
| **Choppity** | Starter $24 / Creator $32 | $16.80 / $22.40 | 3 h / 5 h | $0.133 / $0.093; $0.107 / $0.075 | 45 min | not stated | not stated | yes | Creator+ | MCP page, no plan | [C9] |
| **Munch** | Essential $38 / Premium $60 | same ($456 / $720 per year) | 500 / 1,000 min | $0.076 / $0.060 | 7-day trial | not stated | not stated | yes | not stated | not stated | [C5] |
| **Submagic** | Starter ₹799 / Pro ₹1,999 / Business ₹5,999 | ₹666 / ₹1,666 / ₹4,999 | 5 / 15 / 37 long videos (to 3 h) | ~$0.057 (Starter, 30-min video) | trial only | 3 h | free (Starter), Storyblocks (Pro) | Pro+ | 10-100 min/mo; packs ₹13/min | not mentioned | [C4] |
| Descript (editor) | Hobbyist $24 / Creator $35 | $16 / $24 | 600 / 1,800 media min | $0.040 / $0.027; $0.019 / $0.013 | 60 media min; clips "limited" | - | - | - | API + MCP listed | listed | [C10] |
| Riverside (recording) | Pro $29 / Grow $39 | $24 / $34 | Magic Clips, no cap stated | n/a | watermark | - | - | - | Business | **Grow+** | [C11] |
| Captions / Mirage | Max $24.99 | - | 500 credits; clip burn unpublished | n/a | - | - | - | - | - | - | [C15] second-hand |
| VEED | Pro $45 per seat | $22.08 | 2,500 AI credits | n/a | watermark, ~30 min AI | - | - | - | - | - | [C12] second-hand |
| CapCut | Pro $19.99 | $179.99/yr | long-to-shorts tier unverified | n/a | - | - | - | - | - | - | [C13] second-hand |
| Gling (rough-cut editor) | Plus $20 / Pro $40 | $10 (Plus) | Pro 30 h | Pro $0.022 | 1 h, watermark | - | - | - | - | - | [C14] second-hand |
| **Doomscroller (proposed)** | Creator $10 / Pro $27 / Studio $89 | $8.33 / $22.50 / $74.17 | 1,000 / 3,000 / 10,000 | **$0.010 / $0.0083** (Creator) | 60 once, watermark, 720p | 3 h (free 30 min) | yes | later | **all paid** | **all paid** | |

**Vizard's prices did not render** on its own page, so its row is second-hand, and its upper credit tiers (up to
74,400 credits a year) are unpriced. Those tiers are the one place a lower price could be hiding; check them by
hand before the "cheapest" claim goes on the landing page.

**What this means:**
- **The market floor is WayinVideo Pro yearly at $0.0096/min, but only during its Black Friday sale.** Its own
  page confirms 1 credit = 1 source minute and B-roll on Pro; it has no API below Enterprise. At its $26.99 list
  price the floor would be $0.027. Every other feature-equivalent annual plan is at $0.0167 or more (Reap); the
  market leader, Opus, is at $0.048 annual and $0.10 monthly.
- **MCP is table stakes, not a differentiator.** Vizard and Quso include it on free plans; Reap on every paid
  plan; Opus on Pro; Riverside on Grow. Gating MCP to the top tier, as the contract proposes, would put us behind
  four competitors. Open API + MCP on every paid plan and let credits do the metering.
- **API buyers are the most over-charged segment.** Klap's API costs $0.44 + 8 x ($0.32 + $0.48) = **$6.84** for a
  30-min video with 8 shorts. On Studio we charge 30 credits = **$0.27**.

---

## 4. Cost model

### 4.1 Three stacks, two job shapes

Job shapes:
- **E (owner's 3-clip shape, measured):** 23-min source, 3 clips of ~25 s, B-roll on. Customers may ask for more
  clips than the owner's fixed 3; section 5.9 shows the margin at every clip density.
- **H (headline):** 30-min English source, 8 clips of 30 s, B-roll on.

All three stacks render on the AX42 at 50% average load ($0.000084 per box-second), keep files 30 days on R2,
and add 6% for failed jobs (measured 5.8%).

| | (a) budget | **(c) recommended (hybrid)** | (b) quality, at API prices |
|---|---|---|---|
| STT | Groq turbo, full pass | Groq turbo full pass + **Deepgram Nova-3 on clip windows only** (0.75 min per clip) | Deepgram Nova-3, full pass (Hindi: Deepgram + Sarvam) |
| LLM | Haiku 5.5, all calls | Haiku 5.5, all calls | Sonnet 5.5, all calls (the Claude CLI fallback's model) |
| **H: 30 min, 8 clips** | STT $0.020 + LLM $0.019 + render $0.011 + storage $0.001 = $0.051 x 1.06 = **$0.055** ($0.0018/credit) | STT $0.046 + LLM $0.019 + render $0.011 + storage $0.001 = $0.077 x 1.06 = **$0.082** ($0.0027/credit) | STT $0.129 + LLM $0.388 + render $0.011 + storage $0.001 = $0.529 x 1.06 = **$0.561** ($0.0187/credit) |
| **E: 23 min, 3 clips** | $0.015 + $0.008 + $0.004 + $0.0005 = **$0.029** ($0.0013/credit) | $0.025 + $0.008 + $0.004 + $0.0005 = **$0.039** (**$0.0017/credit**) | $0.099 + $0.159 + $0.004 + $0.0005 = **$0.278** ($0.0121/credit) |

Today's pipeline as actually configured (Deepgram full pass + Claude Haiku 4.5 primary) sits between (b) and (c):
**$0.355** for job H ($0.0118/credit).

**Recommendation: (c).** Captions are where timing shows, and the window pass gives every word on screen
Deepgram's timing for $0.0004 per credit more than (a). Stack (b) cannot be the cheapest product at any margin:
its cost alone ($0.012-0.019 per credit) exceeds our price.

**Hindi** on (c): Groq turbo full pass to pick, plus Sarvam on the clip windows. Hindi uses clean-vlog style with
phrase-timed text, no edit pass and no B-roll. That totals **$0.038 per E job = $0.0017 per credit, the same as
English**. Today's double pass costs $0.0095 per minute for STT alone. Hindi at Sarvam full-pass prices
($0.0063/credit) would leave an 18% margin on Creator annual. The cheap pick pass is what makes Hindi viable at
these prices.

### 4.2 What drives cost (recommended stack, shape E, per credit)

| line | $/credit | share |
|---|---|---|
| Groq pick pass | 0.00067 | 39% |
| Deepgram/Sarvam caption windows | 0.00042 | 25% |
| LLM (Haiku 5.5) | 0.00034 | 20% |
| render + decode (AX42 at 50% load) | 0.00016 | 9% |
| storage (R2, 30 days) | 0.00002 | 1% |
| failed jobs (6%) | 0.00010 | 6% |
| **total** | **0.0017** | |

Payment fees are not in this table; they sit in the margins (6% + $0.40 Dodo, 2.36% Razorpay).

### 4.3 Worst case, and why the credit rule has a second term

**A finished minute costs about 16x a source minute.** A 30 s clip costs $0.0064 (caption window $0.0032, LLM
$0.0017, render $0.0013, storage $0.0002), so a finished minute costs $0.0128. A source minute costs ~$0.0008
(Groq, the moments call and decode).

The contract allows 20 clips of up to 90 s. Take the worst job it allows: 30-min source, 20 x 90 s, B-roll capped
at 6 cutaways per clip.
- **Cost:** STT $0.149 (caption windows cover the whole source) + LLM $0.068 (~257k in / 72k out) + render $0.076
  + storage $0.012 = $0.305 x 1.06 = **$0.323**.
- **At 1 credit per source minute (30 credits):** $0.0108 per credit, which **loses money on every plan**.
- **At `max(source min, 3 x finished min)` = 90 credits:** $0.0036 per credit, a **50% margin** on Creator annual.

Typical jobs never reach the second term. E is max(23, 3.75) and H is max(30, 12): both pay source minutes,
exactly like Opus and Vizard. The estimate endpoint (`POST /api/jobs/estimate`) should show the larger number
before credits are reserved. Also cap B-roll at 6 cutaways per clip in `edit.py`.

### 4.4 Fixed costs, capacity, break-even

| fixed line | $/month |
|---|---|
| Hetzner AX42 (workers, web and DB at launch) | 108.90 (+ $53.90 setup once) |
| Resend Pro (once past 100 emails/day; free before that) | 20 |
| Dodo payout (one monthly SWIFT payout to an Indian bank; the FAQ's $25 non-US rate) | 25 |
| domain, DNS, R2 backups inside the free 10 GB | ~1 |
| Razorpay, Turnstile, Groq, Deepgram, Sarvam, Anthropic | 0 (pay-per-use, no minimums) |
| **total** | **~$155** |

**Contribution of one Creator monthly subscriber at 100% use:** $10 - $1.00 Dodo fee - 1,000 x $0.00154 variable
cost (compute excluded, because it is the fixed server) = **$7.46**. **Break-even: 155 / 7.46 = 21 subscribers.**
Free signups cost $0.09 each (60 credits), so at a 3% conversion rate each paying subscriber carries about $3 of
free-tier cost once.

One AX42 holds ~300 fully-used Creator plans at the heavy shape H, or ~690 at E, before a second box is needed.

Vendor free credits cover the first stretch: Deepgram's $200 [S1] pays for ~46,000 caption-window minutes, about
475k source minutes under the hybrid.

---

## 5. Recommendation

### 5.1 Credit unit
**1 credit = 1 source minute, rounded up per job, with the 3x finished-minute term.** The market already uses the
source-minute unit (Opus, Vizard, WayinVideo, Quso). The finished-minute term is invisible on normal jobs and
stops the one job shape that loses money.

### 5.2 Free tier
**60 credits once per verified identity**, not per email. That is about 2.5 median sources and costs us $0.09.
The contract placeholder of 30 is one median video, below Opus, Vizard and Reap's 60. Conditions:
- **Gating:** granted only when `risk.assess_signup` returns `grant_free_credits` (verified email + Turnstile +
  fingerprint/IP clustering).
- **Output:** watermark on, 720p, at most 3 clips per job, sources up to 30 min, one job at a time, files deleted
  after 7 days, no API.
- **Referrals:** a referral earns +30 credits when the referred person verifies.
- **No monthly refresh.** Recurring free minutes are what farms abuse, and the watermark already advertises us.

### 5.3 Packs (pay-as-you-go, never expire, remove the watermark, usable from a free account)
**250 credits $4 · 1,000 credits $12 · 5,000 credits $49.** The $4 floor keeps Dodo's 40¢ under 16% of the
ticket. Even the smallest pack ($0.016/credit) undercuts every no-commitment competitor rate: WayinVideo Starter
$0.028, 2short Lite $0.033, Reap monthly $0.040.

### 5.4 Subscriptions (3 tiers, credits roll over 1 month)

| plan | monthly | annual | credits | includes |
|---|---|---|---|---|
| **Creator** | $10 | $100 | 1,000/mo | every editing feature (captions, B-roll, SFX, music, memes opt-in), 1080x1920 no watermark, API + MCP, 1 YouTube channel when auto-post ships |
| **Pro** | $27 | $270 | 3,000/mo | Creator + 5 channels, 2 jobs at once, 3 h sources |
| **Studio** | $89 | $890 | 10,000/mo | Pro + priority queue, webhooks, 3 seats, 20 channels, higher API rate limits |

Annual is **2 months free** (pay 10), as the contract already says. A 20% discount would push Pro and Studio
annual under 70% at full use.

### 5.5 India (INR, Razorpay + UPI Autopay, GST included)
**Lite ₹299/300 credits · Creator ₹849 · Pro ₹2,499 · Studio ₹8,399** (same credits as the USD plans); annual =
10 x monthly. Packs: 250 for ₹299, 1,000 for ₹999, 5,000 for ₹3,999.

Our global price is already near PPP level: $10 for 1,000 credits is ₹0.94 per credit. India's discount therefore
comes from three things:
- **GST is included.** A USD checkout adds 18% on top, so Creator is ₹1,109 that way against ₹849 here (23%
  cheaper).
- **Razorpay's 2.36%** replaces Dodo's 6% + 40¢.
- **A ₹299 entry plan.**

Going lower would put Hindi and English minutes under the 70% line. Even so, ₹0.85 per minute is about 6x
cheaper than Submagic's ₹799 for five long videos [C4]. If the owner turns out not to need GST registration (confirm with a CA),
India margins rise by about 15 points.

### 5.6 BYOK (bring your own keys)
**Pro BYOK: $13/mo ($130/yr), 3,000 credits**, for API/MCP developers only, not on the main pricing grid. The
customer plugs in Groq + Deepgram (or Sarvam) + Anthropic keys and pays them about $0.0015 per minute directly. We
keep compute, storage and support ($0.00019/credit), an 87% margin. It answers the "I already have keys" developer
without cutting the price of the main plans.

### 5.7 API and MCP
**Open on every paid plan and on pack balances, at the plan's credit price, with no surcharge.** Studio adds
priority, webhooks and higher rate limits. This contradicts the contract line "Studio includes API + MCP"; see
section 8. The claim we can make: *"$0.27 for a 30-minute video through the API"* against Klap's $6.84.

### 5.8 Refunds and failed jobs

**Credits:**
- **Reservation:** credits are reserved at submit and settled when the job finishes.
- **Failure:** a job that produces no clips releases 100% automatically (5.8% of jobs; already inside the cost
  model).
- **Fewer clips than asked** happened on 21% of the owner's jobs. Those jobs are charged normally, plus **one free
  "find more" run on the same source within 7 days.** The transcript is cached, so it costs only LLM and render,
  about $0.02.

**Money (handled by Dodo/Razorpay as merchant of record):**
- First monthly charge: refundable within 7 days if 60 credits or fewer were used.
- Annual: refundable pro rata within 30 days.
- Packs: refundable within 14 days if untouched.
- Cancel anytime; no proration after those windows.

### 5.9 Gross margin per plan

Columns E and H are at 100% of credits used, after fees (Dodo 6% + $0.40 on subscriptions, 5.5% + $0.40 on
packs; Razorpay 2.36% with 18% GST included in INR prices). Variable cost is $0.0017/credit at E and
$0.0027/credit at H.

| plan | price | E margin | H margin |
|---|---|---|---|
| Pack 250 / 1,000 / 5,000 | $4 / $12 / $49 | 74% / 77% / 76% | 68% / 69% / 66% |
| Creator monthly / annual | $10 / $100 | 73% / 73% | 63% / 61% |
| Pro monthly / annual | $27 / $270 | 74% / 71% | 63% / 58% |
| Studio monthly / annual | $89 / $890 | 74% / 71% | 63% / 58% |
| Pro BYOK | $13 | 87% | 87% |
| India Lite / Creator / Pro / Studio, monthly | ₹299 / 849 / 2,499 / 8,399 | 78% / 75% / 75% / 75% | 67% / 62% / 61% / 61% |
| India same, annual | 10 x monthly | 75% / 71% / 70% / 70% | 61% / 55% / 54% / 54% |

The ≥70% target holds on every plan at the owner's 3-clip shape, even at 100% use. The heavy 8-clip shape runs at
58-69% in USD (57-63% on subscriptions) and 54-67% in INR. Typical credit use is below 100%, which only raises these. If Indian customers
turn out to cut many clips per source, raise India prices first: that is where the stress shape bites hardest.

**Margin by clip density.** Cost per job = 1.06 x ($0.0003 + $0.00073 per source minute + $0.00683 per 30 s
clip). The table gives the clip count per 30 source minutes at which each plan falls to 70%, and the margin at
8 clips.

| plan | 70% holds up to (hybrid) | margin at 8 clips (hybrid) | 70% holds up to (Groq timing only) | margin at 8 clips (Groq only) |
|---|---|---|---|---|
| Creator monthly / annual | 5.0 / 4.9 clips | 62.8% / 61.0% | 9.5 / 9.2 clips | 71.9% / 71.9% |
| Pro monthly / annual | 5.1 / 4.2 | 62.4% / 57.7% | 9.7 / 7.8 | 72.4% / 69.7% |
| Studio monthly / annual | 5.4 / 4.1 | 63.0% / 57.3% | 10.2 / 7.7 | 73.2% / 69.5% |
| Pack 1,000 | 7.3 | 68.5% | 13.7 | 76.1% |

Measure real clip density from the first 100 customer jobs (`meter.summary()`). If the median is above 5 per 30
minutes and the Groq-only caption check has failed, raise annual plans by $1-2 a month rather than cut features.

### 5.10 Below every competitor, and the margin that leaves

| our plan | $/min | cheapest competitor at that commitment | theirs | we are lower by | our margin (E) |
|---|---|---|---|---|---|
| Creator annual | $0.0083 | WayinVideo Pro yearly | $0.0096 | 13% | 73% |
| Pro / Studio annual | $0.0075 / $0.0074 | WayinVideo Pro yearly | $0.0096 | 22-23% | 71% |
| Creator / Pro / Studio monthly | $0.010 / $0.0090 / $0.0089 | 2short Premium (monthly) | $0.0166 | 40-46% | 73-74% |
| smallest pack | $0.016 | WayinVideo Starter monthly | $0.028 | 43% | 74% |

**Two different conflicts, two answers:**
- **A competitor cuts price:** follow it down to $0.0072 per credit, the lowest price that still clears 70% at the
  3-clip shape. Below that, cut cost (self-hosted pick pass, Groq-only timing) rather than margin.
- **Customers cut dense jobs:** cheapest wins and margin falls to 57-63% at 8 clips per 30 minutes (section 0).

---

## 6. The cost of one video

A **30-minute English podcast cut into 8 thirty-second clips with B-roll costs us $0.082**:
- **STT $0.046:** Groq `whisper-large-v3-turbo` on all 30 minutes $0.020, plus Deepgram Nova-3 on the eight clip
  windows $0.026.
- **LLM $0.019:** Claude Haiku 5.5, 43 calls (1 caption check, 1 moment pick over the whole transcript, 1 payoff
  check, 8 edit passes, 32 B-roll judgements), ~80k input and 19k output tokens.
- **Render $0.011:** 128 s of a Hetzner AX42 at half load.
- **Storage $0.001:** 88 MB on R2 for 30 days.
- **Failures:** 6% on top for failed jobs.

It is charged **30 credits**, which earn $0.25 on Creator annual, $0.30 on Creator monthly and $0.48 from the
smallest pack. The measured typical job, a 23-minute vlog with 3 clips, costs **$0.039**.

These figures hold for an upload, or a link the server can download directly. A proxied download would add
$0.17-3.02 (risk 1). The same 30-minute video through today's pipeline at API prices (Deepgram full pass + Claude
Haiku 4.5, its configured primary) would cost **$0.36**, or $0.56 with Sonnet 5.5.

---

## 7. Risks that change the numbers

1. **YouTube blocks the server's IP, so link imports need residential proxies.**
   - **Cost:** residential proxies cost $3.50-8/GB at small volume and $1.40-2/GB at terabytes [S39]. At
     12.6 MB/min a 30-min 720p download is 378 MB, or **$0.53-3.02 per job: 7-37x all other COGS combined.**
   - **Unproven from a server.** The live system's record (1 network failure in 103 jobs) comes from the owner's
     home connection, and CLAUDE.md already documents `COOKIES_FILE` for hosts that hit YouTube's bot check. A
     Hetzner address has never been tried.
   - **Launch default: a home-broadband download relay.** The owner's existing machine runs yt-dlp, which is
     proven, and pushes the file to the AX42 for everything else. This costs nothing extra on an unlimited home
     plan. It is fragile for the same reason the laptop retried 29 jobs: sleep and restarts. So, in order:
     - (a) test 50 links from the AX42 before launch, and switch to direct downloads if they pass;
     - (b) promote uploads, which cost nothing to fetch;
     - (c) if proxies become unavoidable, fetch audio (~1 MB/min) + a 144p analysis stream + 720p sections of the
       chosen clips only (`--download-sections`): ~120 MB instead of 378 MB for job H, ~$0.17 at $1.40/GB;
     - (d) beyond that, charge link imports separately.
   - **Trigger:** this is the only risk here that can erase the margin. Watch the `download` line of
     `meter.summary()`.
2. **Copyright on memes and meme sounds** (section 2.6). Giphy GIFs, MemeGen templates and MyInstants clips are
   third-party works, and Content ID matches audio. A claim lands on the *customer's* channel and becomes churn
   and refunds. Ship them off by default, never in the free tier's sample output, and never in marketing.
3. **Pexels' default 200 requests/hour** caps us at ~6 eight-clip jobs an hour. Request unlimited before launch,
   make Pixabay primary, and share the 24 h search cache across customers.
4. **Groq's cap, repricing or outage.**
   - **The cap:** the Developer plan allows 28,800 audio-seconds a day (~14,400 source minutes a month) [S41].
     Past that, overflow runs on self-hosted faster-whisper (section 2.1).
   - **The fallback we cannot afford:** if the pick pass had to fall back to a Deepgram full pass, E would rise
     from $0.0017 to $0.0051 per credit and Creator annual margin would fall to ~32%. The fallback is self-hosted faster-whisper int8 on the
   AX42's idle threads at ~7.6x realtime [S8], which costs only box time. Keep it as a tested alternate engine.
5. **Hybrid STT quality** (Groq picks, Deepgram or Sarvam captions) is a pipeline change, not yet measured.
   **The price table rests on it.** Before launch, re-run the owner's 36 kept sources (cached transcripts in
   `content/long/`) through the Groq pick pass and compare the chosen moments with today's picks. In the same run,
   compare Groq's word timing with Deepgram's on the chosen clips; if it holds, drop the caption windows (section 0).
   It costs about $0.64 of Groq time. If Hindi picks degrade, use
   `whisper-large-v3` for Hindi ($0.111/h). That raises Hindi to $0.0028 per credit, still above 60% margin.
6. **Haiku 5.5's 100k-token price step:** 5x above 100k input. Chunk long transcripts at 90k (section 2.2).
7. **WayinVideo's promo becomes permanent or deeper.** We have room down to $0.0072 per credit (section 5.10).
8. **Dodo is a young merchant of record** with conflicting payout-fee statements ($5 under $1,000; $25 SWIFT).
   Paddle (5% + 50¢) is the tested-on-paper backup; confirm its India seller onboarding before relying on it.

---

## 8. Changes this implies for other agents

**Contract (integrator and core agent):**
- **Payments:** `POST /webhooks/dodo` replaces `/webhooks/stripe`; keep `/webhooks/razorpay`. Stripe is
  invite-only in India and Lemon Squeezy is closing.
- **Billing rule:** credits charged = `max(ceil(source_minutes), ceil(3 x finished_minutes))`.
  `POST /api/jobs/estimate` returns that number.
- **Free tier:** 60 credits (not 30), gated by `risk.assess_signup`.
- **API + MCP:** open on every paid plan and pack balance, not Studio only.
- **Seed catalog:** load the price table in section 0 into `saas/billing/seed_catalog.json`.

**Pipeline agent (`app/clipper/**`):**
- **STT:** a Groq `whisper-large-v3-turbo` full pass for picking, then Deepgram Nova-3 (English) or Sarvam
  (Hindi) on clip windows only. Respect the job's `language` option, and **stop the Deepgram→Sarvam double call**.
  Add self-hosted faster-whisper (int8) as the overflow engine for Groq's daily audio cap.
- **Validation before launch:** run the 36-source A/B on picks and word timing (section 7, risk 5).
- **Downloads:** link imports go through a download relay on the owner's home connection until 50 links pass
  from the AX42 (section 7, risk 1).
- **AI calls:** call Claude Haiku 5.5 through the API (not the OpenCode/Claude CLIs) at effort `low` for judge and
  edit calls; chunk transcripts at 90k tokens.
- **B-roll:** cap at 6 cutaways per clip.
- **Customer defaults:** memes and MyInstants/trending meme sounds off; remove the Tenor path (API dead since
  2026-06-30).
- **SFX:** ship the CC0 Freesound pack as local files.
- **Metering:** record `stt` per provider and the `download` bytes, so the admin view can check this memo against
  the first 100 real jobs.

---

## 9. Zero-budget launch

The owner's constraints: no cash spend, no registered company, Buy Me a Coffee (BMC) for payments, Cloudflare
free for hosting, and a free tier of **10 shorts or 10 source minutes, whichever comes first**.

**Verdict: launch on free tiers with $0 cash per signup, at about 19 free signups a day.** Groq's free LLM token
budget is the binding limit, not the laptop. The first revenue should buy capacity, not features.

### 9.1 GitHub Actions as a render farm: no

GitHub's official terms, verbatim excerpts from "GitHub Terms for Additional Products and Features", Actions
section [S43]:
- On prohibited uses: "regardless of whether an Action is using self-hosted runners, Actions should not be used
  for:" … "don't use Actions as a content delivery network or as part of a serverless application" … "Any activity
  that places a burden on our servers, where that burden is disproportionate to the benefits provided to users." …
  "If using GitHub-hosted runners, any other activity unrelated to the production, testing, deployment," "or
  publication of the software project associated with the repository where GitHub Actions are used."
- On enforcement: "GitHub may monitor your use." Consequences include "termination of jobs, restrictions in your
  ability to use GitHub Actions," "disabling of repositories created to run Actions in a way that violates these
  Terms," "or in some cases, suspension or termination of your GitHub account."

Rendering customers' videos is unrelated to the repository's software and is part of a serverless application.
Twenty repos created for it is the exact pattern the terms name, and the downside is the owner's GitHub account,
which hosts this codebase.

### 9.2 Cloudflare free plan

**Commercial use is allowed.** The Cloudflare Self-Serve Subscription Agreement (last updated 2025-09-12) has no
personal-use or non-commercial restriction [S44]. Three clauses matter:
- **§2.2.1(h):** we must not "process or collect personal or business credit card information" on a property
  receiving Free Services. BMC's checkout is off-site, so this holds as long as we never take cards ourselves.
- **§2.6 and §8:** Cloudflare may end Free Services "in our sole discretion".
- **CDN video clause** [S45]: Cloudflare "reserves the right to disable or limit … if you use … the CDN without
  such Paid Services to serve video … or a disproportionate percentage of pictures, audio files, or other large
  files." So clips must be delivered from **R2**, never through a proxied tunnel to the laptop.

| service | free limit | source |
|---|---|---|
| Workers + Pages Functions | **100,000 requests/day**, shared between the two (resets 00:00 UTC); 10 ms CPU per invocation; 128 MB memory | [S46][S47] |
| Pages static assets | free and unlimited | [S47] |
| D1 | **5 GB storage, 5 million rows read/day, 100,000 rows written/day**; since 2026-09-01, queries *fail* past the daily caps | [S46] |
| R2 | 10 GB-month, 1M Class A, 10M Class B ops/month, free egress. Activation likely needs a card or PayPal on file ("Purchase R2 Plan"; recent guides, not confirmed on an official page); usage stays $0 | [S22][S48] |
| Turnstile | free, unlimited challenges, 20 widgets | [S27] |
| Zero Trust / Access | free up to **50 users**, $0 "forever" | [S49] |

**Fit:**
- **Pages** serves the site; a **Worker** serves the JSON API and the BMC webhook; **D1** holds accounts, credits
  and jobs. The free `*.pages.dev` / `*.workers.dev` hostnames mean no domain purchase.
- **The laptop polls the Worker for jobs** and uploads finished clips to **R2**.
- **Access** guards `/admin`.
- **Port work:** the current SaaS is FastAPI + SQLite and must be ported to a Worker + D1 (core agent's call).

**R2 caps volume.** At 11 MB per clip, 10 GB holds ~900 clips. A 3-day expiry for free-tier clips supports about
**300 new clips a day**.

### 9.3 Payments: Buy Me a Coffee

| item | fact | source |
|---|---|---|
| fees | **5% platform fee** + Stripe **2.9% + $0.30**, **+1%** outside the US, **+0.5%** on subscriptions, **0.5%** payout fee | [S50] (dated 2026-03-18) |
| what that costs | a $10 international membership loses **$1.29 (12.9%)**, against $1.00 on Dodo. A $100 annual loses $10.20; a $4 one-off loses $0.68 | derived |
| memberships | multiple levels, each with a **monthly and a yearly** price; billed every 30 or 365 days; 10-day grace on failed payments; a lifetime option | [S51] |
| webhooks | 16 events, incl. `membership.started/updated/cancelled/paused`, `extra_purchase.created/refunded`, `donation.created/refunded`; header **`x-signature-sha256`** = HMAC-SHA256 of the raw body with the signing secret; 4 retries; disabled after 10 consecutive failures | [S52] (dated 2026-07-09) |
| India payouts | **India is supported, via Stripe Express** (with Israel, Philippines, South Korea, Türkiye) | [S53] (dated 2026-10-09) |
| without a company | Stripe Connect in India offers an **individual** business type (PAN required). Stripe does not say whether individuals receive cross-border funds | [S54] |

**Wiring:**
- **Plans:** each plan is a BMC membership level with monthly and yearly prices.
- **Packs:** sold as BMC "Extras" (`extra_purchase.created`).
- **Matching:** the Worker matches the supporter's email to the account and grants credits on `membership.started`
  and each renewal (`membership.updated`).
- **Contract change:** `/webhooks/bmc` replaces `/webhooks/dodo` and `/webhooks/razorpay`.

**Test first:** a $1 purchase end to end, to prove an individual PAN-holder actually gets paid out before
launching paid plans.

**Under BMC fees, margins on the section 0 table fall about 3-4 points.** Monthly plans land at ~70%, and annual
plans at 67-69% once paid infrastructure is in use. While everything runs on free tiers, COGS is $0 and margin is
simply 100% minus the BMC fee: 87-90%.

**buymeachai.in:** "We don't take a cut"; money goes "straight to your bank account via UPI. Instant
settlements." [S55]. I found no memberships, API or webhooks, so it cannot grant credits automatically. **Use it
only as a UPI tip jar, not for billing.**

### 9.4 Free compute and AI

| resource | free allowance | catch | source |
|---|---|---|---|
| **Groq Whisper** (turbo / large-v3) | 20 RPM, 2,000 requests/day, 7,200 audio-s/hour, **28,800 audio-s/day (480 min)**, word timestamps | the published table is labelled the Developer plan's base limits; check the console on day one | [S2][S41] |
| **Groq LLMs** (gpt-oss-20b, gpt-oss-120b, qwen3.8-27b) | each: 30 RPM, 1,000 requests/day, **8,000 tokens/min, 200,000 tokens/day** | no vision model in the free table; 8K tokens/min means the moment-pick call must stay under ~8K tokens (cap proposals at 10) | [S41] |
| **Gemini API free tier** | per-model limits shown only in AI Studio; third-party figures range 500-1,500 requests/day for Flash-Lite | **free-tier inputs may be read by human reviewers and used "to improve, and develop Google products"**, and the terms say "Do not submit sensitive, confidential, or personal information". Only paid services may be offered to users in the EEA, Switzerland or the UK; users must be 18+ | [S56][S57] |
| **Deepgram** | **$200 free credit** = ~46,500 Nova-3 minutes, once | not recurring | [S1] |
| **Oracle Cloud Always Free** | Ampere A1: 1,500 OCPU-hours + 9,000 GB-hours a month = **2 OCPUs / 12 GB**; 10 TB egress; 200 GB block storage | **needs a credit card** (no virtual, prepaid or PIN debit); idle instances (p95 CPU, network and memory under 20% for 7 days) may be reclaimed | [S58][S59] |

**Choices:**
- **No Gemini free tier for customer content.** Human review of customers' transcripts and the EEA/UK ban make it
  unfit.
- **Skip Oracle.** It needs a card, adds only 2 Arm cores, and can be reclaimed.
- **Customer AI calls run on Groq's free text models**, rotating across the three.
- **Free tier has no B-roll.** B-roll judging needs a vision model, and without a judge the pipeline already drops
  stock footage. Captions, SFX and music stay on.
- **Keep the Deepgram credit as STT overflow** for paying users.

### 9.5 Capacity and cost per free signup

**Worst-case free signup:** one 10-minute source cut into 10 shorts.

| resource | per signup | daily free budget | signups/day |
|---|---|---|---|
| Groq Whisper | 10 audio minutes | 480 min | **48** |
| Groq LLM tokens | ~30k: moment pick ~8.7k, payoff ~6.8k, 10 edit passes ~15k (incl. reasoning) | 3 models x 200k = 600k | **~19** |
| Laptop render | 10 x 123 s (measured real render per ~25 s clip) + ~27 s decode = **~21 min of CPU** | 12 h/day available (the owner's own growth loop uses ~2 h/day and the laptop sleeps) | **~34** (68 at 24 h) |
| R2 storage | 10 clips x 11 MB, kept 3 days | 10 GB | ~30 |
| D1 / Workers | a few hundred rows and requests | 100k writes, 100k requests | thousands |

**Capacity: ~19 free signups a day,** about 570 a month, bound by Groq's free LLM tokens. If Groq's actual free
limits turn out lower in the console, scale proportionally.

**Cost per free signup: $0 cash.** STT, LLM, hosting and storage all sit inside free tiers. The only physical cost
is ~12 Wh of laptop power per signup (35 W TDP x 21 min, derived) on a machine that already runs 24/7.

**Paid users share the same free budgets.** Groq's 480 audio-minutes/day covers only ~14 fully used Creator plans
a month. Run paid users' STT on the Deepgram credit (~46,500 minutes) and their AI on Groq. **Spend the first BMC
revenue in this order:**
1. Groq paid tier (Whisper at $0.04/hour).
2. Claude Haiku 5.5 API.
3. An AX42 when the laptop's queue wait passes 10 minutes.

### 9.6 What the zero-budget plan changes

**Contract and core agent:**
- Payments: `/webhooks/bmc` (HMAC-SHA256, `x-signature-sha256`) replaces the Dodo and Razorpay webhooks.
- Hosting: the backend moves to a Cloudflare Worker + D1, and the laptop becomes a pull-based render worker.
- Free tier: 10 shorts or 10 source minutes, whichever comes first, no B-roll, 3-day clip expiry.

**Pipeline agent:**
- An engine mode with Groq Whisper (word timestamps) and Groq text models, no vision calls.
- Moment-pick proposals capped at 10 to fit 8K tokens/min.
- Deepgram as overflow for paying users.

**Owner, before launch:**
- Run a $1 BMC test purchase and payout.
- Add a PayPal or card to Cloudflare to activate R2 (stays $0).
- Check Groq's console for the real free-tier limits.

---

## 10. Beta hosting and email

Setup: the FastAPI app runs on the owner's Windows laptop, which renders with ffmpeg and downloads YouTube over
the home IP. The marketing site is on `*.pages.dev`. There is no owned domain and no budget. Invited beta users,
tens a day, need a stable URL.

### 10.1 Cloudflare: tunnels, video, R2

- **Quick Tunnels: not for real users.** "Quick Tunnels are for testing and development." "Each Quick Tunnel
  supports up to 200 in-flight requests. Additional requests return a `429` response." "Quick Tunnels do not
  support Server-Sent Events (SSE)." "Quick Tunnels have no uptime guarantee." [S60] The URL is also random and
  changes on every restart.
- **A named tunnel's public hostname needs a domain on Cloudflare.** Third-party guides say "Cloudflare Tunnel
  requires a domain registered with Cloudflare DNS to create a persistent public hostname" [S61]. Cloudflare's own
  get-started page does not say either way.
- **The way around it is Workers VPC.** A Worker on `*.workers.dev` reaches the laptop through a tunnel created
  for Workers VPC: "Connect a Cloudflare Tunnel to your infrastructure, register each target as a VPC Service",
  then `env.VPC_SERVICE.fetch(...)`. The setup asks only for a tunnel name and the private address; no public
  hostname or domain [S62]. "Available on Free and Paid plans", and "While in beta, Workers VPC is available for
  free to all Workers plans." It "is currently in beta." Requests count against Workers Free: 100,000 a day [S46].
- **Video.** The Service-Specific Terms say: "Unless you are an Enterprise customer, Cloudflare offers specific Paid
  Services (e.g., the Developer Platform, Images, and Stream) that you must use in order to serve video and other
  large files via the CDN." Cloudflare "reserves the right to disable or limit your access … if you use or are
  suspected of using the CDN without such Paid Services to serve video" [S45].
  - Proxying MP4s through a plain tunnel hostname is the restricted case.
  - Serving them through a Worker (the Developer Platform) is the route the clause names. It does not say whether
    the free tier counts, so it is the most defensible $0 route, not a guaranteed one.
  - For tens of 20-40 MB downloads a day, stream them through the Worker.
- **R2 needs a checkout.** Official: "You need a Cloudflare account with an R2 subscription" and "Complete the
  checkout flow to add an R2 subscription to your account"; "R2 is free to get started with included free monthly
  usage" [S63]. The page does not name the payment method. Recent third-party guides say a card or PayPal is
  required at that checkout [S48]. Plan on adding PayPal; usage stays $0 inside 10 GB.

**Recommendation:** `app.<sub>.workers.dev` (Worker) → Workers VPC → `cloudflared` on the laptop → FastAPI.
- The Worker also serves clip downloads: from R2 once a PayPal account is attached, otherwise streamed from the
  laptop.
- Drop the Quick Tunnel and `localhost.run` route from the user path.

### 10.2 Other $0 options

| option | the terms | verdict |
|---|---|---|
| **Tailscale Funnel** | free plan "is only suitable for non-commercial use of Tailscale"; "Traffic sent over a Funnel is subject to non-configurable bandwidth limits"; only ports 443/8443/10000; names under `tailnet-name.ts.net`; "currently in beta" [S64][S65] | **no**: a product beta is commercial use |
| **ngrok free** | "ngrok shows an interstitial page in front of all HTML browser traffic on the free tier"; **1 GB/month** data out; **20,000 requests/month**; one dev domain that "doesn't change when you restart the agent" [S66] | **no**: 1 GB is about 30-50 clip downloads a month, and every user sees a warning page |
| **Cloudflare Worker + Workers VPC (+ R2)** | section 10.1 | **yes**: stable URL, no domain, $0 |

I found no other free option with better terms that I could verify.

### 10.3 Transactional email without a domain

| option | the facts | verdict |
|---|---|---|
| **Gmail SMTP + app password** (a dedicated Gmail account) | "more than 500 emails sent in a day" triggers "You have reached a limit for sending mail", lifted "within 1 to 24 hours" [S67]. "App passwords can only be used with accounts that have 2-Step Verification turned on"; Google says they "aren't recommended"; "we revoke your app passwords when you change your Google Account password" [S68]. Program Policies: "Don't use Gmail to distribute spam or unsolicited commercial mail" (transactional mail to signed-up users is not addressed) [S69] | **recommended** |
| Brevo free | "Brevo also replaces email addresses from free email providers, such as gmail.com, because they cannot be authenticated"; "Domains from free email services … cannot be authenticated" [S70] | no: a gmail.com or proton.me sender cannot pass DMARC through Brevo |
| Proton Mail SMTP | "SMTP is available for all paid Proton Mail plans with custom domain addresses" [S71] | no: needs a paid plan and a domain |
| Resend | "You must add and verify at least one domain to send emails with Resend"; "a domain you own (i.e., not a shared or public domain)" [S72] | no |
| Amazon SES | the sandbox sends "only … to verified email addresses and domains", at most "200 messages per 24-hour period"; production still requires you "verify all identities" used as From [S73] | no: an SES-sent gmail.com From fails DMARC, and gmail.com's policy is quarantine since Feb 2024 per Brevo [S70] |

**Recommendation:** send from `<app>.noreply@gmail.com` through `smtp.gmail.com:587`, with an app password stored
in `.env.saas` on the laptop.
- **Why it lands in the inbox:** Google's own servers send the mail and sign it for gmail.com, so DMARC aligns.
  That is the one $0, domain-less setup that should reach outside inboxes.
- **Volume:** tens a day is well under the 500/day cap.
- **Content:** keep messages transactional only (verification, password reset, job finished).

### 10.4 Buy Me a Coffee webhooks and currency

- **Retries:** "Failed webhook deliveries are retried automatically up to 4 additional times using exponential
  delays"; "If more than 10 consecutive deliveries fail, the webhook is automatically disabled" (with an email).
  The delays are not specified [S52].
- **So the webhook must never point at the laptop.** Point it at the Worker, which verifies `x-signature-sha256`
  (HMAC-SHA256 of the raw body) and writes the event to D1, idempotent on `event_id`. The laptop syncs credits from
  D1, so a sleeping laptop loses nothing.
- **Events for billing:** `membership.started`, `membership.updated`, `membership.cancelled`, `membership.paused`;
  `extra_purchase.created`, `extra_purchase.updated`, `extra_purchase.refunded`. Also available: `donation.*`,
  `recurring_donation.*`, `commission_order.*`, `wishlist_payment.*` [S52].
- **USD while based in India: yes.** "Your account is set to USD by default"; you may "receive support in six
  different currencies"; localized pricing requires "Your currency should be set in USD" [S74]. India is a
  supported payout country through Stripe Express [S53].
  - The currency article does not mention memberships explicitly. Price the levels in USD on the default account
    currency, and confirm with the $1 test purchase.

---

## Sources

Seen 2026-11-27 unless a date is given. "Second-hand" = not the vendor's own page.

**Speech, LLM, compute**
- [S1] Deepgram pricing — https://deepgram.com/pricing
- [S2] Groq speech-to-text docs (prices, word timestamps, 10 s minimum) — https://console.groq.com/docs/speech-to-text
- [S3] AssemblyAI pricing — https://www.assemblyai.com/pricing
- [S4] ElevenLabs API pricing — https://elevenlabs.io/pricing/api
- [S5] OpenAI API pricing — https://developers.openai.com/api/docs/pricing
- [S6] OpenAI speech-to-text guide (timestamps only on whisper-1; until 2027-02-26) — https://developers.openai.com/api/docs/guides/speech-to-text (via search)
- [S7] Sarvam API pricing — https://www.sarvam.ai/api-pricing
- [S8] faster-whisper README benchmark — https://github.com/SYSTRAN/faster-whisper (via search)
- [S9] Anthropic pricing — https://platform.claude.com/docs/en/about-claude/pricing
- [S10] Anthropic vision docs (image token formula) — https://platform.claude.com/docs/en/build-with-claude/vision
- [S11] Gemini API pricing — https://aistudio.google.com/docs/pricing
- [S12] DeepSeek pricing — https://api-docs.deepseek.com/quick_start/pricing
- [S13] OpenRouter fees, second-hand — https://dupple.com/learn/openrouter-pricing ; https://openrouter.ai/docs/faq
- [S14] Hetzner AX matrix — https://www.hetzner.com/dedicated-rootserver/matrix-ax/
- [S15] Hetzner Cloud general purpose — https://www.hetzner.com/cloud/general-purpose/
- [S16] Hetzner Cloud regular performance — https://www.hetzner.com/cloud/regular-performance/
- [S17] Hetzner GEX44/GEX45, second-hand via search — https://www.hetzner.com/dedicated-rootserver/gex44/ ; https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/ ; https://www.hetzner.com/pressroom/hetzner-expands-its-gpu-portfolio-with-the-gex45/
- [S18] AWS c7a.2xlarge, second-hand — https://calculator.holori.com/aws/ec2/c7a.2xlarge ; https://spot.rackspace.com/comparison/aws/ec2/c7a.2xlarge
- [S19] Contabo, second-hand — https://pricingsaas.com/companies/contabo ; https://contabo.com/vps
- [S20] PassMark i5-3320M (2,655, as of 2026-11-02) — https://www.cpubenchmark.net/cpu.php?id=817
- [S21] PassMark Ryzen 7 PRO 8700GE (28,155, snapshot 2026-08-27) — https://www.cpubenchmark.net/compare/6001vs5836/AMD-Ryzen-7-PRO-8700GE-vs-AMD-Ryzen-7-8700G
- [S41] Groq rate limits (Whisper: 20 RPM, 2K RPD, 7.2K audio-s/hour, 28.8K audio-s/day on the Developer plan) — https://console.groq.com/docs/rate-limits
- [S42] OVHcloud Bare Metal 2026, second-hand via search — https://www.ovhcloud.com/en-ie/bare-metal/advance/ ; https://itbrief.news/story/ovhcloud-unveils-bare-metal-2026-servers-with-amd-chips

**Zero-budget section**
- [S43] GitHub Terms for Additional Products and Features, Actions — https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions
- [S44] Cloudflare Self-Serve Subscription Agreement (last updated 2025-09-12) — https://www.cloudflare.com/terms/
- [S45] Cloudflare Service-Specific Terms, CDN (video / large files) — https://www.cloudflare.com/service-specific-terms-application-services/
- [S46] Cloudflare Workers limits and pricing; D1 free-tier enforcement from 2026-09-01 — https://developers.cloudflare.com/workers/platform/limits/ ; https://developers.cloudflare.com/workers/platform/pricing/ ; https://developers.cloudflare.com/changelog/post/2026-09-01-d1-free-tier-limit-enforcement/
- [S47] Cloudflare Pages Functions pricing — https://developers.cloudflare.com/pages/functions/pricing/
- [S48] R2 activation needs a card/PayPal (recent third-party guides, not official) — https://agentdeals.dev/vendor/cloudflare-r2 ; https://dev.to/yeagoo/cloudflare-r2-hands-on-guide-set-up-free-10gb-storage-zero-egress-object-storage-and-325n
- [S49] Cloudflare Zero Trust plans (Free: 50 users, $0) — https://www.cloudflare.com/plans/sase-zero-trust.md
- [S50] Buy Me a Coffee, "How to calculate charges on your payment" (2026-03-18) — https://help.buymeacoffee.com/en/articles/8105744-how-to-calculate-charges-on-your-payment
- [S51] Buy Me a Coffee memberships guides — https://help.buymeacoffee.com/en/articles/9969554-a-simple-guide-to-get-started-with-memberships ; https://help.buymeacoffee.com/en/articles/4368555-how-to-become-a-member (via search)
- [S52] Buy Me a Coffee webhooks (2026-07-09) — https://help.buymeacoffee.com/en/articles/15743173-how-to-setup-and-use-buy-me-a-coffee-webhooks
- [S53] Buy Me a Coffee supported payout countries (2026-10-09) — https://help.buymeacoffee.com/en/articles/6258038-supported-countries-for-payouts-on-buy-me-a-coffee
- [S54] Stripe Connect onboarding requirements in India — https://support.stripe.com/questions/onboarding-requirements-for-stripe-connect-in-india
- [S55] buymeachai.in (official page via search snippet; direct fetch rate-limited) — https://www.buymeachai.in/
- [S56] Gemini API Additional Terms of Service (effective 2026-03-23) — https://aistudio.google.com/docs/terms
- [S57] Gemini free-tier limits (AI Studio only; third-party figures) — https://aistudio.google.com/docs/rate-limits ; https://www.scriptbyai.com/gemini-api-free-tier-limits/
- [S58] Oracle Cloud Always Free resources — https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- [S59] Oracle Cloud Free Tier FAQ (card verification) — https://www.oracle.com/sg/cloud/free/faq/

**Beta hosting and email section**
- [S60] Cloudflare Quick Tunnels (TryCloudflare) — https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/
- [S61] Named tunnels need a Cloudflare DNS domain, third-party — https://localtonet.com/blog/cloudflare-tunnel-alternative ; https://pluggie.io/blog/cloudflare-tunnel-dns-requirement
- [S62] Cloudflare Workers VPC — https://developers.cloudflare.com/workers-vpc/ ; https://developers.cloudflare.com/workers-vpc/get-started/
- [S63] Cloudflare R2 get started — https://developers.cloudflare.com/r2/get-started/
- [S64] Tailscale Funnel — https://tailscale.com/kb/1223/funnel
- [S65] Tailscale pricing / free plan FAQ — https://tailscale.com/pricing
- [S66] ngrok free plan limits — https://ngrok.com/docs/pricing-limits/free-plan-limits/ ; https://ngrok.com/pricing
- [S67] Gmail sending limits — https://support.google.com/mail/answer/22839
- [S68] Google app passwords — https://support.google.com/accounts/answer/185833
- [S69] Gmail Program Policies — https://support.google.com/mail/answer/16734397
- [S70] Brevo domain authentication (free-mail senders replaced) — https://help.brevo.com/hc/en-us/articles/12163873383186-Authenticate-your-domain-with-Brevo-Brevo-code-DKIM-DMARC ; https://help.brevo.com/hc/en-us/articles/16045394674066-Troubleshooting-Domain-authentication ; gmail.com DMARC quarantine (vendor blog): https://www.brevo.com/blog/gmail-and-yahoo-sender-requirements/
- [S71] Proton Mail SMTP submission — https://proton.me/support/smtp-submission
- [S72] Resend domains — https://resend.com/docs/dashboard/domains/introduction
- [S73] Amazon SES sandbox — https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html
- [S74] Buy Me a Coffee advanced settings, currency (2026-08-10) — https://help.buymeacoffee.com/en/articles/10202407-buy-me-a-coffee-advanced-settings-explained

**Storage, email, bot protection**
- [S22] Cloudflare R2 pricing — https://developers.cloudflare.com/r2/pricing/
- [S23] Backblaze B2 pricing — https://www.backblaze.com/cloud-storage/pricing
- [S24] Resend pricing — https://resend.com/pricing
- [S25] Amazon SES pricing (via search) — https://aws.amazon.com/ses/pricing/
- [S26] Zoho ZeptoMail, second-hand — https://www.zoho.com/zeptomail/pricing.html
- [S27] Cloudflare Turnstile plans — https://developers.cloudflare.com/turnstile/plans/

**Payments**
- [S28] Stripe India (invite-only) — https://support.stripe.com/questions/how-can-i-open-a-stripe-account-in-india
- [S29] Stripe Managed Payments pricing — https://support.stripe.com/questions/managed-payments-pricing ; India exclusion second-hand: https://www.paritydeals.com/blog/best-merchant-of-record-for-saas/ , https://tiun.io/blog/merchant-of-record-vs-payment-facilitator-stripe
- [S30] Razorpay pricing — https://razorpay.com/pricing/
- [S31] Paddle pricing — https://www.paddle.com/pricing ; India buyer support: https://developer.paddle.com/concepts/sell/supported-countries-locales
- [S32] Lemon Squeezy (no new signups, shutting 2027-03-31) — https://www.lemonsqueezy.com/pricing
- [S33] Dodo Payments pricing — https://dodopayments.com/pricing ; India payouts, vendor-stated: https://dodopayments.com/blogs/get-paid-usd-developer-india

**Content APIs, proxies, FX**
- [S34] Pexels API documentation — https://www.pexels.com/api/documentation/
- [S35] Pixabay API docs (24 h cache; 100 req/60 s from the archived 2022 snapshot) — https://pixabay.com/api/docs/
- [S36] Tenor API shutdown — https://www.heise.de/en/news/Google-cuts-off-Tenor-access-Apps-must-replace-GIF-search-11350334.html ; https://gigazine.net/gsc_news/en/20260701-tenor-api-discontinuation/
- [S37] Giphy production key — https://support.giphy.com/hc/en-us/articles/360035158592-What-conditions-does-my-app-project-need-to-meet-in-order-to-get-a-production-API-Key
- [S38] Freesound API terms — https://freesound.org/help/tos_api/
- [S39] Residential proxy $/GB, second-hand (snapshots Sep-Oct 2026) — https://webscraping.ai/blog/cheapest-residential-proxies ; https://hproxy.com/blog/residential-proxy-price-study-2026
- [S40] FX — USD/INR 93.5 on 2026-11-18: https://therighttrader.com/convert/1-usd-to-inr ; EUR/USD ~1.10 on 2026-11-17 to 19: https://www.xe.com/currencycharts/?from=USD&to=EUR

**Competitors**
- [C1] Opus Clip — https://www.opus.pro/pricing (plans and features, 2026-11-27); credits second-hand: https://www.castmagic.io/blog/opus-clip-pricing (verified 2026-08-20), https://www.stork.ai/blog/best-ai-video-clippers-2026 (checked 2026-11-03)
- [C2] Vizard — https://vizard.ai/pricing (limits and features; prices did not render); prices second-hand: https://contenttoolslab.com/reviews/vizard-ai-pricing/ , https://www.openshorts.app/vizard-ai (early Nov 2026)
- [C3] Klap — https://klap.app/pricing (page updated 2026-11-04)
- [C4] Submagic — https://www.submagic.co/pricing (INR view)
- [C5] Munch — https://www.munchstudio.com/pricing
- [C6] Quso — https://quso.ai/pricing
- [C7] 2short.ai — https://www.2short.ai/pricing
- [C8] Spikes Studio — https://spikes.studio/pricing
- [C9] Choppity — https://www.choppity.com/pricing
- [C10] Descript — https://www.descript.com/pricing
- [C11] Riverside — https://riverside.com/pricing (page flagged itself as staging)
- [C12] VEED, second-hand — https://www.veed.io/pricing/plans.md (via search)
- [C13] CapCut, second-hand — https://www.capcut.com/help/how-much-does-capcut-pro-cost (via search; snapshot 2026-11-12)
- [C14] Gling, second-hand — https://www.xpay.sh/saas-pricing/gling-ai/ , https://aisotools.com/pricing/gling
- [C15] Captions / Mirage, second-hand — https://www.hooked.so/compare/captions-ai-pricing
- [C16] WayinVideo — https://wayin.ai/pricing/ (own page, rendered in a browser 2026-11-27: Black Friday 65% off, Starter $5.58 / Pro $9.58 / Scale $69.99 per month billed yearly, 6,000 / 12,000 / 72,000 credits a year, "1 credit processes 1 min of source video"); plans: https://intercom.help/wayinvideo/en/articles/15692322-how-wayinvideo-plans-work ; credits: https://intercom.help/wayinvideo/en/articles/15913299-how-credits-work ; earlier snapshot: https://www.stork.ai/blog/best-ai-video-clippers-2026 (checked 2026-11-03)
- [C17] Reap — https://reap.video/pricing
