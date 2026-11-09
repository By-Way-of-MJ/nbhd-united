# Rival app review mining — NBHD

**Prepared:** 2026-11-09 · **Our app:** NBHD — *AI Journal & Life Coach - NBHD* (iOS `6779158519`)  
**Scope:** 10 closest rivals, App Store storefronts US + JP, 1–2★ reviews only, 1,287 captured low reviews.

> **Read this first.** The public customer-reviews RSS feed exposes at most 10 pages × 50 reviews per storefront, so this is the **most recent 500 reviews per app per country**, not an all-time census. “Low reviews” below means 1–2★ reviews captured from that window. Ratings and rating counts are lifetime App Store totals. No reviewer names were stored.

## 1. The 10 rivals

Selection: candidates from the 9 required searches in US + JP plus `similar` expansion for the top 3 search hits, filtered to apps whose core job is journaling, mood/self-care journaling, or AI life coaching; then ranked by US rating count and capped at 10. Apple Journal, Habit Tracker, Fabulous, and note-taking apps were excluded as adjacent rather than same-job. All 10 are free downloads monetised by subscription.

| # | Rival | Developer | App Store ID | Rating | Rating count | Last update | Months since update | Price model | Low reviews captured (US/JP) |
|---:|---|---|---:|---:|---:|---|---:|---|---|---:|
| 1 | Finch: Self-Care Pet | Finch Care Public Benefit Corporation | `1528595748` | 4.95 | 773,515 | 2026-11-06 | 0.1 | Free + Finch Plus subscription | 49 (46/3) |
| 2 | Day One: Daily Journal & Diary | Bloom Built Inc | `1044867788` | 4.83 | 118,915 | 2026-10-23 | 0.6 | Free tier + Silver/Gold subscription | 238 (112/126) |
| 3 | Reflectly - Journal & AI Diary | Kodeon, Inc. | `1241229134` | 4.57 | 81,676 | 2026-09-18 | 1.7 | Free + Reflectly Premium subscription | 88 (81/7) |
| 4 | DailyBean - simplest journal | BlueSignum Corp. | `1553223828` | 4.81 | 71,074 | 2026-11-07 | 0.1 | Free + premium subscription; ad-supported | 138 (41/97) |
| 5 | Daylio Journal - Mood Tracker | Relaxio s.r.o. | `1194023242` | 4.77 | 62,394 | 2026-11-05 | 0.1 | Free + premium subscription | 50 (31/19) |
| 6 | Liven: AI Life Coach | Chesmint limited | `6450840109` | 4.45 | 51,969 | 2026-11-05 | 0.1 | Free trial → subscription (website/Apple) | 487 (221/266) |
| 7 | stoic. journal & mental health | Stoic app inc. | `1312926037` | 4.82 | 36,245 | 2026-11-04 | 0.2 | Free + Stoic Premium subscription | 82 (80/2) |
| 8 | Lightly: Wellbeing Companion | Reface Lithuania UAB | `6575383588` | 4.78 | 5,172 | 2026-11-07 | 0.1 | Free + subscription | 66 (65/1) |
| 9 | Rosebud: AI Journal & Diary | Just Imagine | `6451135127` | 4.88 | 3,394 | 2026-11-07 | 0.1 | Free + subscription (limited free tier) | 47 (47/0) |
| 10 | Hapday: AI Life Coach & CBT | HAPDAY - FZCO | `1498572982` | 4.78 | 972 | 2026-11-04 | 0.2 | Free preview + subscription | 42 (18/24) |

**Captured low reviews:** 1,287 across 10 apps (US 742 / JP 545).

Footnotes: Liven’s US storefront shows 4.45 from 51,969 ratings; its JP storefront shows 3.74 from 4,486. Price models are inferred from the listing copy and the reviews themselves — the iTunes lookup API returns price 0 for all ten and does not expose in-app-purchase SKUs. “Months since update” is days from 2026-11-09 ÷ 30.44.

## 2. Top 5 product complaints (price excluded)

Product themes only: **bugs, data loss, AI quality, UX, content/localization**. Price/subscription is separated into §3; ads (72) and customer support (32) are monetisation/service issues, not product-quality themes, and are excluded from this ranking. The next product theme is feature gaps/removals (49). If ads were counted as product, ads (72) would enter at #3; we treat it as a revenue mechanic, not a quality defect.

| Rank | Product complaint | Count | Share of low reviews | Hit hardest |
|---:|---|---:|---:|---|
| 1 | **Bugs, crashes & broken core flows** | **244** | 19.0% | Day One: Daily Journal & Diary 89; Liven: AI Life Coach 53; DailyBean - simplest journal 27 |
| 2 | **Data loss, sync & backup failures** | **83** | 6.4% | Day One: Daily Journal & Diary 32; DailyBean - simplest journal 17; Reflectly - Journal & AI Diary 6 |
| 3 | **AI quality, memory & forced AI** | **61** | 4.7% | Liven: AI Life Coach 16; Day One: Daily Journal & Diary 15; stoic. journal & mental health 9 |
| 4 | **UX/UI, navigation & redesigns** | **56** | 4.4% | Liven: AI Life Coach 24; Day One: Daily Journal & Diary 16; Daylio Journal - Mood Tracker 7 |
| 5 | **Content quality & localization** | **51** | 4.0% | Liven: AI Life Coach 44; Day One: Daily Journal & Diary 3; stoic. journal & mental health 2 |

### 1. Bugs, crashes & broken core flows — 244 low reviews

*What it is:* Won’t open, freezes, crashes, features stop working (search, photos, keyboard, playback), broken onboarding.  
*Hit hardest:* Day One: Daily Journal & Diary (89); Liven: AI Life Coach (53); DailyBean - simplest journal (27); stoic. journal & mental health (18); Finch: Self-Care Pet (16).

> I’ve tried offloading memory, I’ve tried rebooting and re-downloading the app, I’ve updated the app (again today! When the fix they promised was released this week!) and it still freezes my phone when I open the app and start an entry, and then takes forever to unlock enough to type.
> — (Day One: Daily Journal & Diary, review_id `13905181193`)

> I downloaded this app and upon opening for the first time, it crashed.
> — (stoic. journal & mental health, review_id `13974797466`)

> Select your egg color and after that just a white screen that never loads.
> — (Finch: Self-Care Pet, review_id `14701819528`)

### 2. Data loss, sync & backup failures — 83 low reviews

*What it is:* Entries disappear, sync overwrites newer work, reinstall wipes history, backup/restore fails.  
*Hit hardest:* Day One: Daily Journal & Diary (32); DailyBean - simplest journal (17); Reflectly - Journal & AI Diary (6); stoic. journal & mental health (6); Daylio Journal - Mood Tracker (5).

> アップデートの途中で停止状態のアイコンになったまま、タップしてもアプリが開かなくなりました。仕方なくアプリ削除して再インストールしたところ、これまでのすべてのジャーナルデータが消えてしまった初期状態で起動しました。
> — (Day One: Daily Journal & Diary, review_id `12415990569`)

*JA: “While updating, the app got stuck on the loading icon and would not open. I deleted and reinstalled it, and it started from scratch with all of my journal data gone.”*

> After I recently updated the app and forced to re login, ALL MY DATA ARE LOST!
> — (DailyBean - simplest journal, review_id `13115887980`)

> Today it stopped saving my entries. It would go to reflect with AI and delete my entire entry! It did this not once but TWICE!
> — (Lightly: Wellbeing Companion, review_id `12844388052`)

### 3. AI quality, memory & forced AI — 61 low reviews

*What it is:* Generic or repetitive answers, forgotten context, hallucinated/gaslighting responses, AI pushed into the journal and not optional.  
*Hit hardest:* Liven: AI Life Coach (16); Day One: Daily Journal & Diary (15); stoic. journal & mental health (9); Rosebud: AI Journal & Diary (8); Lightly: Wellbeing Companion (6).

> Even with AI features toggled ‘off’, the app pushes suggestions to use AI and attempts to upsell you to the highest subscription plan that uses AI.
> — (Day One: Daily Journal & Diary, review_id `13954819359`)

> It’s like I’m talking to a person with dementia or Alzheimer’s. Every day it forgets and confuses basic details.
> — (Rosebud: AI Journal & Diary, review_id `14431786960`)

> Liven completely downgraded their awesome AI coach — to the point that she’s almost cold, doesn’t remember me well, and gives only short replies.
> — (Liven: AI Life Coach, review_id `14230119478`)

### 4. UX/UI, navigation & redesigns — 56 low reviews

*What it is:* Confusing navigation, tiny fonts, cluttered UI, redesigns that make the app harder, endless scrolling.  
*Hit hardest:* Liven: AI Life Coach (24); Day One: Daily Journal & Diary (16); Daylio Journal - Mood Tracker (7); stoic. journal & mental health (3); Reflectly - Journal & AI Diary (2).

> But the interface on an iPhone is abysmal if you have a long journal. It’s constantly bringing you to the top and you have to scroll all the way down to get to earlier entries.
> — (Day One: Daily Journal & Diary, review_id `14145899532`)

> I would very likely purchase this app if the font weren’t so small and difficult to read.
> — (Reflectly - Journal & AI Diary, review_id `12298811601`)

> Also the UI with all those multiple options doesn’t really help track trends. You don’t simplify it!
> — (Daylio Journal - Mood Tracker, review_id `13540988653`)

### 5. Content quality & localization — 51 low reviews

*What it is:* Machine-translated or broken Japanese/other languages, missing language support, weak lesson/content quality.  
*Hit hardest:* Liven: AI Life Coach (44); Day One: Daily Journal & Diary (3); stoic. journal & mental health (2); Hapday: AI Life Coach & CBT (1); Lightly: Wellbeing Companion (1).

> 日本語訳が雑過ぎて何を言っているのかさっぱりわからない。
> — (Liven: AI Life Coach, review_id `14052669974`)

*JA: “The Japanese translation is so sloppy that I have no idea what it is saying.”*

> この内容のアプリを、機械翻訳に丸投げしただけでリリースするのは、開発者の道徳観を疑います。
> — (Liven: AI Life Coach, review_id `13783336804`)

*JA: “Releasing an app with this content after just handing it to machine translation makes me question the developer’s ethics.”*

> Где русский язык?
> — (stoic. journal & mental health, review_id `14760834135`)

*RU: “Where is the Russian language?”*

## 3. Price, subscription & billing complaints

**434 of 1287 low reviews (33.7%) are about price, subscription, billing, cancellation, refunds, free-trial traps or paywalls.** A further **72** are about intrusive ads (DailyBean 46, Liven 14, Reflectly 7).

| App | Price low reviews | Share of that app’s low reviews | App Store rating | Rating count |
|---|---:|---:|---:|---:|
| Liven: AI Life Coach | 231 | 47% | 4.45 | 51,969 |
| Day One: Daily Journal & Diary | 33 | 14% | 4.83 | 118,915 |
| Reflectly - Journal & AI Diary | 33 | 38% | 4.57 | 81,676 |
| Lightly: Wellbeing Companion | 33 | 50% | 4.78 | 5,172 |
| DailyBean - simplest journal | 28 | 20% | 4.81 | 71,074 |
| stoic. journal & mental health | 25 | 30% | 4.82 | 36,245 |
| Rosebud: AI Journal & Diary | 18 | 38% | 4.88 | 3,394 |
| Hapday: AI Life Coach & CBT | 14 | 33% | 4.78 | 972 |
| Daylio Journal - Mood Tracker | 12 | 24% | 4.77 | 62,394 |
| Finch: Self-Care Pet | 7 | 14% | 4.95 | 773,515 |

**Do people pay anyway?** Yes — the apps with the loudest billing complaints still carry very large lifetime rating counts: Liven 51,969 at 4.45, Day One 118,915 at 4.83, Reflectly 81,676 at 4.57, DailyBean 71,074 at 4.81, Lightly 5,172 at 4.78. Liven is the outlier where billing anger has visibly pulled the average down, but it still has a large installed and paying base. Treat rating count as scale/install evidence, not as proof that current subscribers are happy: App Store ratings are cumulative and the low-review window is recent.

The pattern is consistent across apps: annual plans with no monthly option, trials that convert without a clear warning, cancellation buried outside the app (especially Liven’s website subscription), duplicate charges, and basic features moved behind the paywall. These are monetisation complaints, not product-quality complaints, and they are the easiest thing for NBHD to avoid being compared on.

## 4. Opening verdict

**1. Day One: Daily Journal & Diary — reliability is the wedge.**  
Day One produced 238 low reviews, the second-largest pool, and 89 are bugs plus 32 data-loss/sync complaints — roughly half of its low reviews are “the core act of journaling broke.” Be the journal that cannot lose a thought: local-first writes, atomic autosave, one-tap export, offline capture, conflict-safe sync recovery, and no AI in the compose path unless the user asks. One line: **match Day One’s polish, beat it on data safety and capture speed.**

**2. Rosebud: AI Journal & Diary — grounded memory is the wedge.**  
Rosebud’s low-review pool is small (47) but the product complaint is pointed: 8 AI-quality complaints, including forgetting stable facts, generic/“word salad” replies, and therapy-voice overreach. Be the AI journal that remembers deliberately, cites the user’s own entries, exposes an editable memory, refuses to role-play a therapist, and never rewrites or deletes an entry on its own. One line: **make the AI feel accountable to the journal, not the other way round.**

Liven has more absolute product complaints (53 bugs, 44 content/localization, 24 UX), but its low-review pool is dominated by billing anger (231 price reviews). That makes it a pricing cautionary tale more than a product-opening target. The cleanest product openings are Day One (reliability) and Rosebud (AI trust).

## 5. Reddit: “alternative to <rival>”

Reddit blocks anonymous API/HTML fetches from this environment (HTTP 403), so the thread URLs and titles below were captured from Brave/Yahoo search-result indexes; thread bodies were not opened. No URL was invented. Queries were the exact phrase `"alternative to <rival>"` plus `reddit`.

### Finch: Self-Care Pet

- [Alternatives to Finch?](https://www.reddit.com/r/finch/comments/1oxm2dq/alternatives_to_finch/)
- [Finch Alternatives?](https://www.reddit.com/r/finch/comments/1twtpgd/finch_alternatives/)
- [Looking for alternative options🙏🏼](https://www.reddit.com/r/finch/comments/1qtzdiv/looking_for_alternative_options/)
- [Alternatives](https://www.reddit.com/r/finch/comments/1q36wxs/alternatives/)
- [Apps like Finch?](https://www.reddit.com/r/finch/comments/1k4hfgt/apps_like_finch/)

### Day One: Daily Journal & Diary

- [Looking for a Day One alternative](https://www.reddit.com/r/dayoneapp/comments/1c7uhbw/looking_for_a_day_one_alternative/)
- [Alternatives to Day One journal app?](https://www.reddit.com/r/ios/comments/a2zfev/alternatives_to_day_one_journal_app/)
- [Alternative to Day One](https://www.reddit.com/r/digitaljournaling/comments/1fvwt2e/alternative_to_day_one/)
- [Has anyone switched from Day One to another app? Why?](https://www.reddit.com/r/digitaljournaling/comments/1cjsmgn/has_anyone_switched_from_day_one_to_another_app/)
- [My Replacement for Day One After The Price Changes](https://www.reddit.com/r/dayoneapp/comments/1scd8hg/my_replacement_for_day_one_after_the_price_changes/)

### Reflectly - Journal & AI Diary

**none found** for the exact phrase `"alternative to Reflectly"` — the exact-phrase search returned only blog listicles, no Reddit thread. A broader search surfaced these adjacent real threads (not exact-phrase matches), listed for completeness:

- [What are the most similar Daylio alternatives](https://www.reddit.com/r/Daylio/comments/d1a9nl/what_are_the_most_similar_daylio_alternatives/)
- [I need help finding a journaling app](https://www.reddit.com/r/NoStupidQuestions/comments/1dpfuxa/i_need_help_finding_a_journaling_app/)
- [which of these would be best to use? I want to log a ...](https://www.reddit.com/r/ipad/comments/mwcg2i/which_of_these_would_be_best_to_use_i_want_to_log/)
- [Any other Reflectly users here? Am I missing something?](https://www.reddit.com/r/ios/comments/csd9oj/any_other_reflectly_users_here_am_i_missing/)

## 6. Japan note

JP contributed 545 of the 1287 low reviews (US 742). The shape is meaningfully different:

- **Bugs dominate JP far more than US:** 152 JP low reviews are bugs (28% of JP) vs 92 US (12%). Day One alone contributes 67 JP bug reviews out of 126 JP low reviews — Japanese users are hitting a broken build/update, not just a pricing wall.
- **Localization is a JP-specific product complaint:** 47 JP low reviews are content/localization (8.6% of JP) vs 4 US (0.5%). 44 of the 47 JP content complaints are Liven, complaining about machine-translated Japanese, broken layout/characters, and English-only lesson content.
- **Price is still large but a smaller share:** 141 JP low reviews are price/billing (26% of JP) vs 293 US (39%). Liven accounts for 106 of the JP price complaints; US Liven reviewers are even more billing-focused (125 of 221).
- **AI-specific complaints are much rarer in JP:** 4 JP vs 57 US. Japanese reviewers are more focused on the app working and being written in natural Japanese than on AI persona/quality.

Practical read: for a JP launch, “AI journal” will not rescue a broken or machine-translated product. Natural Japanese UI copy, Japanese-language AI output, and update reliability matter more than AI feature breadth. The JP reviews also show lower tolerance for subscription opacity, but they punish broken core flows first.

## 7. Method, files & limits

- **Search:** 9 required terms × US/JP = 18 searches, 25 results each; `similar` for the top 3 search hits in both storefronts; 382 unique candidates before same-job filtering.
- **Reviews:** Apple RSS, pages 1–10 per app per country (`sortby=mostrecent`), deduped by review id; only 1★/2★ stored. 1,287 low reviews across 10 apps. No usernames stored.
- **Tags:** one theme per review, 12 themes + `price` + `other` (14 total). Rule-based classifier with title-first rules, Japanese/Chinese/Russian patterns, and manual spot checks; counts are read from `tags.csv`, never estimated.
- **Quotes:** every quote in this file is asserted to be an exact substring of the stored `text` before writing; `check_quotes.py` re-checks all 15 and prints PASS/FAIL per quote.
- **Files:** `raw-low-reviews.json`, `tags.csv`, `RIVALS.md`, `check_quotes.py`.
- **Limits:** 500-review RSS cap per storefront; low-star sample skews recent; App Store lifetime ratings are not a current-satisfaction measure; IAP prices/tiers are not exposed by the lookup API; Reddit bodies were not fetchable (403), only search-index titles/URLs; JP review volume is uneven across apps (Liven 266, Rosebud 0, Lightly 1).

### Acceptance outputs

```text
$ python3 -c "import json;d=json.load(open('docs/research/rivals-2026-11/raw-low-reviews.json'));print(len(d), len({x['app_id'] for x in d}))"
1287 10

$ python3 -c "import csv,collections;print(collections.Counter(r['theme'] for r in csv.DictReader(open('docs/research/rivals-2026-11/tags.csv'))).most_common())"
[('price', 434), ('bugs', 244), ('other', 117), ('data_loss', 83), ('ads', 72), ('ai_quality', 61), ('ux', 56), ('content', 51), ('feature_gap', 49), ('account', 32), ('support', 32), ('performance', 22), ('notifications', 20), ('audio', 14)]
```
