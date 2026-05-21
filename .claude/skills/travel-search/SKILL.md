---
description: "Travel & expense management: flights, hotels, car hire via APIs and browser. TRIGGER when: user asks to find/search/compare flights, hotels, accommodation, car rental, travel options, trip planning, route optimization, cheapest dates, or anything related to booking corporate travel. ALSO trigger for ANY informal travel reference like 'search San Diego again', 'find flights', 'ищи перелеты', 'снова ищи', etc. — even in Russian or without explicit travel keywords. Also trigger for: kiwi search-flight, google_flights_search/google_flights_search_dates tools, or Google Hotels/Cars browser searches."
---

# Travel & Expense Management — Corporate Flights, Hotels, Car Hire

You have multiple travel search tools for corporate travel management. Use the right combination for each task.

## Available Sources

### ✈️ Flights

| Source | Tool | Coverage | Best For |
|--------|------|----------|----------|
| **Kiwi.com** | `search-flight` (MCP: kiwi-flights) | ~750 airlines, LCCs, self-transfers | Creative routing, multi-carrier combos, booking links |
| **Google Flights** | `google_flights_search`, `google_flights_search_dates` (custom MCP tool, fli engine) | All major airlines | Price comparison, cheapest dates across range |
| **Aviasales** | Camoufox browser (`aviasales.ru`) | Russian + CIS airlines, full intl coverage | Flights to/from Russia, CIS routes, Russian carriers (Aeroflot, S7, Pobeda, etc.) |
| **BA Avios** | Playwright (`awardtravelfinder.com`) + Camoufox (`ba.com`) | BA network, oneworld partners | Reward flights, companion voucher availability |
| **Entravel** | Playwright browser (`entravel.com`) | Major airlines, OTA-negotiated fares | Detailed booking stage — may have cheaper fares for specific flights |
| **Expedia TAAP** | Playwright browser (`expediataap.com`) | All major airlines | Agent net rates (after commission), package deals |
| **Camoufox browser** | Manual via `camofox_*` tools | Any airline/aggregator website | Direct airline booking, award seats, anti-bot sites |

### 🏨 Hotels — Google Hotels + Booking.com + Expedia TAAP + Entravel + TripAdvisor

No API — use Playwright browser automation to search Google Hotels for initial discovery.
For shortlisted hotels, compare prices across ALL four sources:
1. **Google Hotels** — aggregated OTA prices, initial discovery
2. **Booking.com** — direct prices, reviews, availability
3. **Expedia TAAP** — agent net rates (after commission rebate) — often cheapest
4. **Entravel** — members-only discounted rates (login required)
Cross-reference ratings on Booking.com and TripAdvisor for quality assurance.

**HOTEL FILTERS — loaded from stored facts at runtime:**
Check facts for keys like `hotel_min_stars`, `hotel_min_booking_score`, `hotel_min_tripadvisor_score`.
If no facts are set, use sensible defaults (no filtering). The skill itself is generic —
all instance-specific preferences live in the knowledge graph, not hardcoded here.

### 🚗 Car Hire — Google Cars + Expedia TAAP via Playwright

No API — use Playwright browser automation to search Google car rental.

---

## Proactive Search Behavior — MANDATORY

These rules apply to ALL travel searches. Be proactive — don't wait for the user to ask.

### 0a. VIP / Fast Track / Skip-the-Line — ALWAYS CHECK AND RECOMMEND

⛔ **NEVER book standard/basic tickets for ANY attraction, tour, cable car, train, museum,
theme park, or experience without FIRST researching premium options.**

This is the #1 comfort rule — learned from a painful queue experience at Bondinho (Sugarloaf).

**MANDATORY for EVERY bookable activity:**
1. **Research VIP/Fast Track options** — search "[attraction] fast track / VIP / skip the line / priority access"
2. **Strongly recommend premium tickets** when available — present both options (standard vs VIP)
   with price comparison, but DEFAULT recommendation = VIP/fast track
3. **Set booking deadlines** — popular slots sell out. Note when booking opens and set
   a reminder to book early (e.g. "Fast Pass for Bondinho sells out on weekends — book 2+ days ahead")
4. **Analyze peak times** — recommend off-peak time slots to minimize crowds even WITH fast track
5. **Check all premium tiers** — some attractions have multiple levels (standard → fast track → VIP → private)

**This applies to ALL trip planning, not just travel:**
- Theme parks (Disney, Universal, etc.) → Express Pass / Lightning Lane / VIP tours
- Museums → timed entry, skip-the-line, private guided tours
- Cable cars, funiculars, observation decks → priority boarding, VIP access
- Trains (scenic, historic) → first class, priority boarding
- Restaurants → priority seating, chef's table, tasting menus
- Tours → private vs group, small group upgrades
- Airports → fast track security, lounge access, priority boarding

**Price is NOT the deciding factor** — the organization values productivity and efficiency over marginal savings.
Always present the premium option first. If the user wants to economize, they will say so.

**Booking deadline workflow:**
- When planning any activity: immediately check if advance booking is needed
- Set a reminder/deadline BEFORE the booking window closes
- For popular attractions in peak season: book as early as possible, don't wait

### 0b. Verify Hotel/Resort Operational Status BEFORE Suggesting

⛔ **NEVER suggest a hotel or resort without first verifying it is currently open and operating.**

Hotel and resort closures happen frequently — resorts close permanently, rebrand, or go under renovation. Presenting a closed property as an option wastes the user's time and destroys trust.

**MANDATORY check before including ANY hotel/resort in suggestions:**
1. **Search the web** for "[Hotel Name] closed" or "[Hotel Name] [year] open" to confirm current operational status
2. **Check the hotel's official website** — a redirect, 404, or "temporarily closed" page is a red flag
3. **Check reviews** — the most recent TripAdvisor/Booking.com reviews will mention if the property has closed
4. If ANY doubt: do NOT include in suggestions. Mention why you excluded it.

**This applies especially to:**
- All-inclusive resort chains (Club Med, Mark Warner, TUI, etc.) — they close/rebrand individual properties regularly
- Resorts in regions with recent instability or natural disasters
- Properties you haven't personally confirmed recently (rely on web search, not training data)

**Example of what went wrong:** Club Med Kamarina (Sicily) was presented as an option — it had permanently closed. A 10-second web search would have caught this.

### 0c. PROACTIVE EXPERIENCE & COMFORT OPTIMIZATION — MANDATORY

⛔ **NEVER present a bare-bones itinerary.** Every destination answer MUST include researched,
curated experience recommendations that maximize experience quality and team comfort.

This rule applies to ALL travel planning responses — city visits, day trips, resort stays,
multi-day itineraries. It is NOT optional and NOT limited to "when the user asks for activities."

**CORE PRINCIPLE: Immersive > Observation > Walking**

For every attraction, landmark, or natural wonder, research the BEST way to experience it:
- 🚤 **Boat trip at Iguaçu Falls** (feel the water on your skin) > helicopter tour > walking trail
- 🚁 **Helicopter over Rio** > viewpoint visit > photo from street level
- 🛥️ **Speedboat to Sugarloaf** > cable car > taxi to base
- 🎭 **Private guided tour** with insider stories > audio guide > self-guided walk
- 🍽️ **Cooking class with local chef** > restaurant dinner > food court

Always rank experiences by IMMERSION LEVEL and present the most immersive option first.
The team values experiencing the place, not just seeing it. Price is NOT the deciding factor.

**1. SIGNATURE EXPERIENCE RESEARCH — for every destination:**

Before presenting ANY itinerary or day plan, research and include:
a) **Iconic must-do experiences** — the 3-5 things this place is famous for, in their BEST form
   (not generic tourist version). Search: "[destination] best experiences", "[destination] unique activities",
   "[landmark] best way to visit", "[attraction] boat/helicopter/VIP tour"
b) **Hidden gems** — experiences most tourists miss but locals love.
   Search: "[destination] hidden gems", "[destination] off the beaten path group",
   "[destination] locals recommend"
c) **Experience quality hierarchy** — for each activity, identify ALL available formats
   (walking, boat, helicopter, private, group, VIP) and rank by immersion level.
   Present the best format first with price, then alternatives.
d) **Team suitability** — age-appropriate? Stroller-accessible? Nap-friendly schedule?
   Safe for baby? Engaging for 7-year-old?

**2. WEATHER & CONDITIONS ANALYSIS — for every outdoor plan:**

NEVER suggest outdoor activities without checking:
a) **Seasonal weather patterns** — search "[destination] weather [month]", check historical averages.
   Note: temperature, rain probability, humidity, UV index, wind.
b) **Time-of-day conditions** — morning fog at waterfalls? Afternoon thunderstorms in tropics?
   Sunset timing for golden-hour experiences? Midday heat in summer?
c) **Adaptive planning** — for EACH outdoor activity, have a rain/bad-weather alternative:
   "If it rains: [indoor alternative]. If clear: [original plan]."
d) **Clothing & gear alerts** — "Bring waterproof jackets for boat ride", "Wear closed shoes
   for trail", "Sunscreen essential — no shade for 2 hours"
e) **Seasonal closures & conditions** — trails closed in rainy season? Beach dangerous in winter?
   Water too cold for swimming? Jellyfish season?

**3. TIMING & LOGISTICS OPTIMIZATION — think through EVERY detail:**

a) **Optimal visit times** — research when each attraction has shortest queues, best light,
   best atmosphere. Search: "[attraction] best time to visit", "[attraction] avoid crowds".
   Cross-reference with opening hours, last entry times, and transit schedules.
b) **Geographic clustering** — group activities by area. NEVER zigzag across a city.
   Check actual distances (Google Maps) and realistic travel times including traffic.
c) **Buffer time** — add 30-60 min buffers between activities. Account for:
   transit delays, bathroom breaks with kids, unexpected photo ops, slow restaurant service.
d) **Energy management** — alternate high-energy activities with rest/food/gentle activities.
   Don't stack 3 exhausting activities in a row. Plan a sit-down break every 2-3 hours.
e) **Meal timing** — research and pre-book restaurants near activity locations.
   Don't leave meals to chance — hangry kids ruin trips. Search for suitable
   restaurants with reviews mentioning fast service, high chairs, dietary options.
f) **Nap/baby logistics** — if Baby needs a nap, plan a calm midday segment
   (hotel return, long restaurant lunch, scenic drive where baby sleeps).
g) **Sunset/golden hour** — always know sunset time. Plan the most scenic activity
   for golden hour. Note: "[Viewpoint X] faces west — perfect for sunset at [time]."

**4. NUANCE & PRACTICAL DETAIL — mine reviews for actionable tips:**

For every recommended activity, restaurant, or transport:
a) **Read recent reviews** (last 6 months) on TripAdvisor, Google Maps, blogs.
   Search: "[activity/restaurant] reviews [year]", "[activity] tips", "[activity] what to know before going"
b) **Extract practical tips** that most guides miss:
   - "Bring cash — card machine often broken"
   - "Arrive 15 min before stated opening — they let people in early"
   - "Sit on the LEFT side of the boat for the best waterfall view"
   - "Book the 8am slot — afternoon groups are 3x larger"
   - "The restaurant's Instagram vs reality — skip the steak, order the fish"
c) **Booking logistics** — for each bookable activity:
   - WHERE to book (official site? Viator? GetYourGuide? Direct at venue?)
   - HOW FAR in advance (sells out 2 weeks ahead? Walk-up OK?)
   - CANCELLATION policy (free cancel 24h? Non-refundable?)
   - Include the booking URL
d) **Transport between activities** — don't just say "go to X". Specify:
   - How to get there (taxi/Uber/metro/walk), estimated time, estimated cost
   - Which taxi app works in this city (Uber? Bolt? local app?)
   - "Walk from restaurant to cable car = 12 min uphill — consider taxi with baby"
e) **Cost estimates** — for EVERY recommendation, include price per person or per group.
   Don't suggest "take a boat tour" without "~£40/adult, £20/child, free under 3"

**5. COMPARATIVE OPTIONS — always present choices:**

For each major activity slot, present 2-3 options ranked by experience quality:
```
🥇 BEST: Macuco Safari speedboat INTO the falls — £50/person, 20 min, get soaked,
   unforgettable. Book 3 days ahead on macucosafari.com.br. Kids 3+ only.
🥈 GREAT: Gran Aventura — 4x4 jungle drive + boat ride near falls, £40/person.
   Less intense, better for younger kids. Walk-ups usually OK.
🥉 GOOD: Walking trails (free with park entry) — Garganta del Diablo trail is the
   must-do. 1.1km boardwalk, stroller-friendly. Go at 8am opening to avoid crowds.
```

This lets the team choose their adventure level without missing the best option.

### 0d. TEAM COMPOSITION AWARENESS — Adapt Plans to WHO is Travelling

⛔ **NEVER plan a trip without first identifying the travel party composition and roles.**
The same destination requires COMPLETELY different planning depending on who's going.

**STEP ZERO for every trip plan:** Check stored facts and conversation context to determine:
- WHO is travelling? (Parent1+Parent2 only? With Child1+Child2? With baby Baby? All five?)
- WHAT are the children's ages at the time of travel? (Calculate from DOB in stored facts)

**Three distinct planning modes:**

#### MODE A: SMALL TEAM (1-2 travelers)
Maximum freedom. Optimize for:
- 🍷 **Fine dining** — Michelin-star, tasting menus, late reservations (21:00+), wine pairings
- 🏔️ **Adventure activities** — helicopter tours, extreme sports, cliff hikes, long treks
- 🕐 **Flexible timing** — no nap windows, no early bedtimes, sunrise-to-midnight days OK
- 🏨 **Hotel choice** — boutique/romantic, rooftop bar, couples spa, no need for team/group rooms
- 🚗 **Spontaneous routing** — scenic detours, winding mountain roads, no car seat logistics
- 🎭 **Evening activities** — shows, concerts, nightlife, sunset cocktail bars, late walks
- ⚡ **Pace** — can pack more per day, longer walking distances, skip rest breaks
- No stroller access considerations, no child menus, no height/age restrictions

#### MODE B: FULL TEAM (3-5 travelers)
Active group mode. Balance activity intensity with team energy levels:
- 🎢 **Age-appropriate activities** — check height/age minimums for EVERY activity:
  - Child1 (8): most activities OK, can handle moderate hikes (5-8km), boats, bikes
  - Child2 (5): more limited — check "minimum age 6/7/8" restrictions carefully
  - ALWAYS note restrictions: "Minimum age 7 — Child2 cannot participate"
- 🍽️ **Group-friendly restaurants** — dietary options, high chairs, not too formal, reasonable wait times
  (max 15-20 min for food). Avoid 2hr tasting menus. Lunch by 12:30, dinner by 18:30-19:00.
- 🕐 **Energy management** — alternate active/calm activities. Max 2 high-energy activities/day.
  Kids crash after 6-7 hours of touring. Plan a sit-down break every 2 hours.
- 🏨 **Hotel needs** — team/group rooms or 2 connecting rooms. Pool is ESSENTIAL (kids swim daily).
  Kids club is a bonus. Breakfast included saves morning hassle.
- 🚶 **Walking limits** — Child2: max ~3-4km continuous walking. Child1: ~5-6km.
  Plan transport between distant attractions. Consider hop-on-hop-off for city touring.
- 🎮 **Engagement factor** — activities must be FUN for the team, not just scenic for adults.
  Interactive > passive. Hands-on > look-and-learn. Animals, water, playgrounds = winners.
- 📱 **Downtime** — kids need screen/play time. Don't schedule every minute.
  Leave 1-2 hours unplanned per day for pool time, playground, or just relaxing.
- 🧳 **Logistics** — who carries the backpack with snacks/water/sunscreen/spare clothes?
  Note where bathrooms are along the route.

#### MODE C: LARGE GROUP (6+ travelers or special requirements)
Everything changes. Comfort and logistics become priority #1:
- 👶 **Nap schedule is LAW** — midday nap 12:00-14:30 is non-negotiable.
  NEVER schedule activities during nap time. Plan morning block (09:00-12:00) and
  afternoon block (15:00-18:00) with hotel/quiet return in between.
- 🍼 **Feeding logistics** — where to warm bottles? High chairs at restaurants?
  Baby-friendly food options? Supermarket nearby for supplies (nappies, formula, snacks)?
- 🚼 **Stroller access** — for EVERY recommended place, verify: Is it stroller-friendly?
  Cobblestones? Stairs? Lifts? Steep hills? If not: "Leave stroller, use carrier."
  Search: "[attraction] stroller accessible", "[venue] pram friendly"
- 🚗 **Car seat** — rental cars need infant car seat. Verify included or bring own.
  Taxis/Ubers: car seat availability varies by country — note local rules.
- 🏨 **Hotel must-haves** — cot/crib availability (book in advance!), baby-safe room
  (no sharp edges, balcony with low rail = danger), baby bath, bottle steriliser.
  Ground floor preferred (no lift waits with screaming baby). Late checkout valuable.
- ⏰ **Pace: SLOW** — max 2 activities per day. Buffer 50% more time for everything.
  Baby meltdowns happen. Nappy changes happen. Feeding takes 30 min.
  What takes adults 3 hours takes 5 hours with a baby.
- 🏥 **Safety net** — nearest hospital/pharmacy for each area. Paediatric emergency
  number for the country. Bring first aid basics.
- 🎯 **Activity filtering** — verify all activities match the group composition:
  - Boat tours: life jacket for infants? Shade on boat? Loud engine?
  - Cable cars: crowded cabin with stroller? Queue in sun for 45 min?
  - Museums: quiet policy? Baby noise = dirty looks?
  - Hiking: carrier-friendly trail or rocky terrain?
  For each activity, explicitly note: "Baby-friendly: ✅/⚠️/❌" with reason.
- 👫 **Split strategy** — sometimes the best plan is: one parent does the adventure
  activity with older kids, other parent stays with baby at hotel/park/beach.
  Suggest this when an activity is clearly not baby-suitable.
- 🌡️ **Temperature sensitivity** — babies overheat fast. No midday sun activities in
  hot climates. Shade, hydration, and cool spaces are mandatory.

**ALWAYS state your planning mode explicitly:**
"Planning in MODE B (with Child1 8 and Child2 5, no Baby)" — so the travelers can correct
if you guessed wrong about who's coming.

**When the travel party is unclear:** ASK before planning. Don't waste time building
an adults-only itinerary if the kids are coming, or a baby-safe plan when it's just
Parent1 and Parent2. One question upfront saves a complete rewrite.

### 1. Full Date Coverage for Flexible Dates

When the user says "any dates", "flexible", "best dates", "optimal", or specifies a date RANGE:
- ALWAYS search ALL dates in the range systematically — not just spot-check a few
- For flights: search every day across the range (or use `google_flights_search_dates`
  for the full range, falling back to individual day searches if that tool errors).
  When using individual day searches, batch in groups of 8 (see Google Flights rate limit below).
  Build a complete **price map by date** and present it to the user.
- For hotels: search the cheapest check-in windows, not just one arbitrary date set
- For multi-leg trips: optimize the TOTAL trip cost across all legs combined,
  not each leg in isolation. The cheapest outbound + cheapest return may fall on
  different days — find the combination that minimizes total spend.
- Present a clear "price by date" summary so the requester sees the full landscape
  and can make an informed choice — not just "here are 3 options I picked"
- When one leg has dramatically different pricing by day (e.g. return flights varying
  £874–£5,152 across the month), highlight this explicitly — it drives the date choice

### 2. Proactive Criteria Clarification

When search results are broad or the user hasn't specified key constraints,
PROACTIVELY suggest narrowing criteria — don't drown the user in 50 options:

**Key criteria to clarify (pick the 2-3 most impactful for the search):**
- ✈️ Direct only or connections OK? If connections: max layover duration?
- 💺 Cabin class: Economy / Premium Economy / Business / First?
- 💷 Pay cash or use Avios (BA reward miles)? Companion voucher available?
- ⏰ Preferred departure window? (morning / afternoon / evening / red-eye OK?)
- 🏨 Minimum star rating? Budget per night? Must-have amenities (pool, breakfast)?
- 🚗 Car type? Manual or automatic? Insurance included?

**Rules:**
- Don't ask all questions at once — pick the ones that will cut options the most
- If the user has expressed preferences before (stored in facts), apply them
  automatically without re-asking. E.g. if "dad prefers daytime flights" is
  stored, filter for daytime by default and note it.
- If a search returns >10 viable options at similar prices, narrow down BEFORE
  presenting — suggest filters, or group into "budget / mid-range / premium" tiers
- Always state which criteria/filters you applied so the user knows what was excluded

---

## Flight Search Workflow

### ⚡ MANDATORY: ALWAYS search Kiwi + Google Flights IN PARALLEL

For EVERY flight search, launch BOTH sources simultaneously using parallel tool calls.
This is NOT optional — each source has different coverage and pricing:
- Kiwi finds creative self-transfer combos and LCC routes Google misses
- Google Flights shows direct airline pricing that can be cheaper than Kiwi's OTA prices
- Prices can differ 10-40% between sources for the same route

**For Russia routes:** Add Aviasales as a THIRD parallel source (see Aviasales section below).
Google Flights has limited coverage of Russian carriers due to sanctions/geo-restrictions.
Aviasales is essential for VKO/SVO/DME routes and Russian domestic airlines.

**How to parallelize:** Call `search-flight` (kiwi-flights MCP) and `google_flights_search` (custom MCP tool)
in the SAME tool call block. They have zero dependency on each other.
For Russia routes, also spawn an Agent to search Aviasales via Camoufox in parallel.

**⚠️ Google Flights rate limit — MAX 8 parallel requests:**
Google Flights server-side limit is ~9 concurrent requests (10th gets 429'd).
Safe batch size: **8 parallel `google_flights_search` calls** per batch.
- If searching N dates: split into batches of 8, wait for each batch to complete before the next
- Example: 20 dates → batch 1 (8) → batch 2 (8) → batch 3 (4). ~8-10 seconds total.
- Kiwi `search-flight` has NO such limit — fire all Kiwi calls freely in any batch
- When mixing Google + Kiwi in one batch, count only the Google calls toward the limit of 8
- `google_flights_search_dates` counts as 1 call (server handles the range internally)

After all sources return, MERGE results:
1. De-duplicate same flights (match by airline + times)
2. When same flight appears in multiple sources — show the cheaper price, note all sources
3. Flights unique to one source — include with source label
4. Sort by user preference (price/time/convenience)
5. Present top 5-7 with booking links from the cheapest source

### Quick single-route search:
1. **Parallel:** Kiwi `search-flight` + Google `google_flights_search` (same dates, same route)
   - Add Aviasales via Camoufox if route involves Russia/CIS
2. Merge results, de-duplicate, pick cheapest source per flight
3. Present with booking links

### Date-flexible search:
1. **Parallel:** Google `google_flights_search_dates` (cheapest dates across range) + Kiwi for the most likely dates
2. Once cheapest dates identified, run Kiwi on those specific dates for creative routing
3. Cross-reference prices (+ Aviasales for Russia routes)

### Complex multi-city / round-trip:
1. **Parallel:** Kiwi (excels at multi-carrier creative routing) + Google (each leg separately)
2. Compare total cost: Kiwi's combined itinerary vs Google's individual legs

### Kiwi `search-flight` parameters:
- `origin`, `destination` — IATA codes (LHR, VKO, IST, etc.)
- `departure_date` — YYYY-MM-DD
- `return_date` — for round-trips
- `cabin_class` — ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST
- `adults`, `children`, `infants` — passenger counts
- `max_stops` — 0 (direct), 1, 2
- `currency` — GBP, EUR, USD, etc.

### Google Flights `google_flights_search` parameters:
- `origin`, `destination` — IATA codes (e.g. "LHR", "SAN")
- `departure_date` — YYYY-MM-DD
- `return_date` — optional, for round-trips
- `cabin_class` — ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST (string)
- `max_stops` — 0 (Any), 1 (Non-stop), 2 (1-stop), 3 (2-stops)
- `sort_by` — CHEAPEST, TOP_FLIGHTS, DEPARTURE_TIME, ARRIVAL_TIME, DURATION
- `adults` — number of adult passengers (default 1)
- `children` — number of child passengers (default 0)
- `max_results` — max flights to return (default 10)
- `currency` — GBP, EUR, USD etc. (default GBP)

### Google Flights `google_flights_search_dates` parameters:
- `origin`, `destination` — IATA codes
- `start_date`, `end_date` — date range to search (YYYY-MM-DD)
- `trip_duration` — days (for round-trips)
- `is_round_trip` — boolean
- `cabin_class` — ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST
- `max_stops` — 0 (Any), 1 (Non-stop), 2 (1-stop), 3 (2-stops)
- `adults` — number of adult passengers (default 1)
- `currency` — GBP, EUR, USD etc. (default GBP)

---

## Aviasales Search Workflow — via Camoufox Browser

**WHEN TO USE:** For ANY flight involving Russia or CIS countries (VKO, SVO, DME, LED,
and all Russian domestic airports). Google Flights has poor coverage of Russian carriers
due to sanctions and geo-restrictions. Aviasales is THE primary aggregator for these routes.

**Also useful for:** comparing international prices — Aviasales sometimes shows cheaper
fares from Russian OTAs not visible on Google/Kiwi.

### Step 1: Build the Aviasales URL
```
URL format (direct search results):
https://www.aviasales.ru/search/{origin}{departure_DDMM}{destination}{return_DDMM}{passengers}

Examples:
One-way VKO→LHR on March 16:
https://www.aviasales.ru/search/VKO1603LHR1

Round-trip VKO→LHR Mar 16, return Apr 3:
https://www.aviasales.ru/search/VKO1603LHR0304161

URL param breakdown:
- {origin} — 3-letter IATA code
- {departure_DDMM} — day+month, e.g. 1603 = March 16
- {destination} — 3-letter IATA code
- {return_DDMM} — day+month for return (omit for one-way)
- {passengers} — last digit: 1 adult = "1", 2 adults = "2"
```

### Step 2: Navigate with Camoufox
Use Camoufox (NOT Playwright) — Aviasales uses anti-bot detection.
```
1. Create a fresh Camoufox tab (UK preset)
2. Navigate to the search URL
3. Wait 10-15 seconds for results to load (AJAX-heavy, loads progressively)
4. Take snapshot to extract flight data
```

### Step 3: Parse results
Aviasales shows results with:
- Airline name + flight number
- Departure/arrival times with timezone
- Duration and number of stops
- Layover airports and duration
- Price in RUB (convert to GBP for comparison)
- "Buy" button linking to the OTA with best price

### Step 4: Currency conversion
Aviasales prices are in RUB. Convert to GBP for comparison with Kiwi/Google results.
Use approximate rate or check xe.com for current rate.

### Anti-bot notes:
- Use Camoufox, NOT Playwright (Aviasales blocks headless Chromium)
- Create fresh tab for each search
- Wait for full page load (results load progressively via AJAX)
- No CAPTCHA typically, but Camoufox stealth patches are needed
- If blocked, try with residential proxy (configure in Camoufox env vars)

### Russian carrier IATA codes:
- SU — Aeroflot
- S7 — S7 Airlines (Siberia)
- DP — Pobeda (low-cost)
- UT — UTair
- U6 — Ural Airlines
- N4 — Nordwind
- 5N — Nordavia / Smartavia
- IO — IrAero
- GH — Globus
- FV — Rossiya

---

## Entravel Flight Price Verification — via Playwright Browser

**WHEN TO USE:** At the **detailed booking stage** — when you have identified specific flights
(airline + route + dates) from Kiwi/Google and the user is ready to book. Entravel may offer
OTA-negotiated fares that are 5-25% cheaper than direct airline or standard OTA pricing.

**NOT for initial search** — Entravel doesn't have a flight aggregator/search engine. Use it
only to verify pricing on specific flights already found via Kiwi/Google/Aviasales.

### Step 1: Log in to Entravel
```
URL: https://entravel.com
Credentials: stored in facts (entravel_email, entravel_password)
```
1. Navigate to `https://entravel.com` with Playwright
2. Log in using stored credentials (check facts for `entravel_email` and `entravel_password`)
3. If already logged in (session persists in browser profile), skip login

### Step 2: Search for the specific flight
1. Use the flight search on Entravel — enter origin, destination, dates, passengers
2. Look for the EXACT flights identified from Kiwi/Google (match by airline + departure time)
3. Extract Entravel prices for matching flights

### Step 3: Compare and report
1. Compare Entravel price vs cheapest price found on Kiwi/Google
2. If Entravel is cheaper — highlight the saving (amount + percentage)
3. If Entravel doesn't have the flight or is more expensive — note this briefly
4. Include Entravel booking link if it's the cheapest source

### Integration with flight search workflow:
- Run Entravel verification AFTER the initial Kiwi + Google parallel search
- Spawn an Agent to check Entravel in parallel with other verification tasks (Aviasales, BA Avios)
- Only check the top 2-3 flight options (not all results)
- Prices may be in USD — convert to GBP for comparison

### Anti-bot notes:
- Entravel works with both Playwright AND Camoufox (no anti-bot detection)
- Browser profile persists login session — may not need to re-authenticate each time
- IMPORTANT: See "Entravel Hotel Search — Pitfalls & Working Approach" for date/room caching issues
- Rate limit: reasonable delays between searches (2-3s)

---

## Entravel Hotel Search — Pitfalls & Working Approach

Entravel is a React SPA with aggressive client-side state caching. Multiple non-obvious
behaviors cause "0 rooms available" even when rooms exist. Follow these rules exactly.

### ⚠️ CRITICAL PITFALLS

**PITFALL 1 — URL params are IGNORED for dates:**
Navigating to `/hotel/{id}?s_dates=20260404,20260409` does NOT set the search dates.
The SPA reads dates from its internal session/cookie state, not from the URL.
The URL gets rewritten to reflect the cached session dates, not your requested dates.
**Result:** "0 rooms available" because the cached dates may have no inventory.

**PITFALL 2 — Children in search params → 0 rooms:**
If children were ever included in search parameters during the session (even in a previous
search), Entravel caches them in cookies. Many hotels then return 0 rooms.
**ALWAYS search as "2 Adults" only**, never add children. Room types that support
children (Group Room, Extra Bed variants) are listed in the results automatically.

**PITFALL 3 — Cached session state persists across tabs:**
Camoufox shares cookies/session across tabs for the same userId. If one tab searched with
children or different dates, new tabs inherit those cached params. Changing the URL does NOT
override the cached state.

**PITFALL 4 — Search from homepage vs hotel page:**
Searching from the homepage redirects to search results. Clicking a hotel card from search
results DOES properly transfer the dates (SPA router handles it). But direct navigation
to a hotel URL does NOT properly set dates.

### ✅ WORKING WORKFLOW — Hotel Room Extraction

**Step 1: Navigate to Entravel and verify login:**
```
1. Navigate to https://entravel.com
2. Check if "Account" shows profile name (= logged in)
3. If not logged in: click Account → login with stored credentials
```

**Step 2: Navigate to hotel page (any method):**
```
1. Navigate to: https://entravel.com/hotel/{hotel_id}
   (dates in URL will be IGNORED — that's OK, we fix it in Step 3)
```

**Step 3: Use the date picker UI to set correct dates (THIS IS MANDATORY):**
```
1. Click the dates field to open the calendar picker:
   - Use JS: document.querySelector('[class*="fieldButton"]').click()
   - Or click the element containing "Dates:" text
2. In the calendar: click the CHECK-IN date first, then click the CHECK-OUT date
3. Verify the date picker shows correct range (e.g. "5 nights stay")
4. Click "Search" button on the hotel page
5. Wait for "rooms available" text to appear
```
This forces the SPA to update its internal state with the new dates and re-fetch room
inventory from the API. This is the ONLY reliable way to set dates.

**Step 4: Extract room data from snapshot:**
The "View rooms" section will now show available rooms with:
- Room name and type (Deluxe, Premium, Family, Suite, etc.)
- Size in m²
- Price per night and total (including taxes)
- "Extra Bed" variants indicate child-friendly rooms
- "Group Room" variants are purpose-built for groups (typically 40m²)

**Step 5: Identify team-suitable rooms:**
Look for these room types (in order of preference for 2 adults + 2 children):
1. **Group Room (2Ad+2Ch)** — explicitly designed, best fit
2. **Junior Suite (ExtraBed 2Ad+2Ch)** — larger, often similar price
3. **Premium Room (Extra Bed 2Ad+1Ch)** — works if kids can share a bed
4. **2 × Deluxe Room** — fallback: two separate rooms

### Currency
- Entravel shows prices in the user's selected currency (EUR/GBP/USD)
- Currency can be changed via the "€ EUR" / "£ GBP" button in the header
- For comparison: always convert to GBP (the organization's base currency)

### Key Technical Details
- Hotel IDs are numeric (e.g. The Marker Dublin = 5921132)
- Camoufox works well (no anti-bot issues)
- Playwright also works — either browser is fine
- Login session persists in Camoufox cookies — often no re-login needed
- The `es=` URL parameter contains a base64-encoded JSON with SearchId, PropertyId,
  Query (Residency, Occupancies, CheckIn, CheckOut) — but it's generated server-side
  after a successful search, not something you can construct manually

---

## Expedia TAAP — Travel Agent Affiliate Program

**WHAT IT IS:** Expedia's B2B platform for travel agents. Provides agent net rates
(wholesale prices after commission) for hotels, flights, car hire, and packages.
The account is via Fora Travel agency (Platinum tier), giving access to best commission tiers.

**WHEN TO USE:**
- Hotel price comparison (shortlisted hotels) — compare TAAP agent net price vs Booking/Entravel
- Flight searches — alternative pricing source
- Car hire — often competitive rates through agency bulk deals
- Package deals (flight + hotel) — TAAP bundles can be significantly cheaper

**PRICING MODEL — THREE PRICE TIERS:**
TAAP shows three prices for each room/service:
1. **Published price** — the retail price a customer would see on Expedia.com
2. **Agent commission** — percentage or fixed amount the agent earns
3. **Agent net price** — published price minus commission = what the agent actually pays

**⚠️ CRITICAL: Always use the AGENT NET PRICE for comparison**, not the published price.
The organization can book through the agent account and get the net price (keeping the commission).
When presenting results: show both published and net prices, highlight the saving.

### Browser & Login

**Default browser: Playwright** (expediataap.com works fine with standard Chromium).
Switch to Camoufox only if Playwright gets blocked (unlikely — it's a B2B portal, not anti-bot).

**Credentials:** stored in facts (`expedia_taap_email`, `expedia_taap_password`).
```
URL: https://www.expediataap.com/
```

**Login workflow:**
1. Navigate to `https://www.expediataap.com/`
2. If login page: enter credentials from stored facts
3. Session persists in Playwright browser profile — often already logged in
4. Verify: look for agent name / "Platinum" badge in header

### Hotel Search on TAAP

**URL format (direct search):**
```
https://www.expediataap.com/hotels?q={destination}&startDate={YYYY-MM-DD}&endDate={YYYY-MM-DD}&rooms=1&adults=2
```

**Step-by-step:**
1. Navigate to TAAP hotel search or use direct URL
2. Enter: destination, check-in/out dates, guests (2 Adults — same as Entravel, no children)
3. Find the target hotel in results
4. Click into hotel detail page
5. Extract ALL room types with three price columns:
   - Published price (retail)
   - Commission amount/percentage
   - **Agent net price** (this is what we compare)
6. Note: cancellation policy, breakfast inclusion, room size

**Data extraction via `browser_evaluate`:**
```javascript
// Extract room data from TAAP hotel detail page
// Adapt selectors based on actual page structure
document.querySelectorAll('[data-stid="section-room-list"] .uitk-card').forEach(card => {
  // room name, published price, commission, net price
})
```

### Flight Search on TAAP

Navigate to flights section. Enter origin, destination, dates, passengers.
TAAP shows published fares with agent markup/commission info.
Compare the net fare (after commission) with Kiwi/Google results.

### Car Hire on TAAP

Navigate to car rental section. Enter pickup location, dates.
TAAP often has bulk-negotiated rates through agency partnerships.
Compare with Google Cars results.

### TAAP Pitfalls:
- Prices are in USD by default — always convert to GBP for comparison
- "Non-refundable" rates are cheapest but can't be changed
- Refundable rates are typically $10-20 more
- Breakfast is usually extra ($30-60/person) — factor into total comparison
- Room availability can differ from Booking.com (different inventory pools)
- Agent net price = final price only if no credit card FX fees (pay in USD)

---

## Currency Conversion — MANDATORY for Price Comparisons

When comparing prices across sources, each may use a different currency:
- **Google Hotels** — uses the currency parameter (default GBP)
- **Booking.com** — EUR or local currency
- **Entravel** — EUR, GBP, or USD (user-selectable)
- **Expedia TAAP** — USD (agent net prices always in USD)
- **Kiwi.com** — configurable (default GBP)
- **Google Flights** — configurable (default GBP)

### Conversion Rules:
1. **ALWAYS convert all prices to GBP** before comparison
2. **Use live exchange rates** — fetch from xe.com or Google:
   - Quick method: `browser_evaluate` on Google search `"1 EUR in GBP"` or `"1 USD in GBP"`
   - Or use WebSearch for current rates
3. **Show the conversion explicitly** in comparison tables:
   ```
   Entravel: €2,156 (≈£1,815 @ 0.842)
   TAAP net: $2,300 (≈£1,820 @ 0.791)
   Booking:  €1,969 (≈£1,658 @ 0.842)
   ```
4. **Include the exchange rate used** so the user can verify
5. **For close prices (within 3%)**: note that FX fluctuation may change the ranking
6. **Credit card FX fees**: factor in ~1.5-3% FX markup for non-GBP payments
   (some cards have 0% FX fee — check stored facts for `fx_free_card` preference)

### Rate Freshness:
- Fetch exchange rates at the START of each comparison session
- Reuse the same rates within one comparison (consistency)
- Note approximate rates are fine — precision to 3 decimal places is sufficient

---

## BA Avios Reward Flights — Two-Level Search

British Airways reward flights (Avios) and companion voucher availability require
a two-level approach: quick scan for availability, then targeted verification on ba.com.

### Level 1 — Quick Scan via AwardTravelFinder (FREE, no login)

**Use for:** broad search across destinations/dates to find where reward seats exist.
Fast, free, shows a full year of availability at once. No CAPTCHA, no login needed.

**Tool:** Playwright browser (NOT Camoufox — simple site, no anti-bot)

**URL format:**
```
https://awardtravelfinder.com/airlines/british-airways?origin={IATA}&destination={IATA}
```

**Examples:**
```
All BA destinations from LHR:
https://awardtravelfinder.com/airlines/british-airways?origin=LHR

LHR → specific destination:
https://awardtravelfinder.com/airlines/british-airways?origin=LHR&destination=JFK
```

**Workflow:**
1. Navigate to AwardTravelFinder with origin (typically LHR)
2. Snapshot the page — shows calendar grid with availability by class:
   - Economy (green = available, red = none)
   - Premium Economy
   - Business (Club World)
   - First
3. Parse availability dates by cabin class
4. Present: "BA reward seats available LHR→JFK: Economy on [dates], Business on [dates]"

**For multi-destination scan ("where should we go?"):**
1. Run Level 1 for top 20-30 popular destinations from LHR in parallel (use Agent tool)
2. Collect availability by class for each
3. Present summary table: destination | Economy | Business | First | Avios cost
4. User picks interesting options → proceed to Level 2 verification

**Avios pricing reference (peak/off-peak per person, return):**
| Zone | Economy | Biz | First |
|------|---------|-----|-------|
| Europe short | 13K-26K | 26K-52K | — |
| Europe long | 20K-40K | 40K-80K | 60K-120K |
| North America | 26K-52K | 52K-104K | 68K-136K |
| Middle East | 20K-40K | 40K-80K | 50K-100K |
| Asia | 39K-78K | 78K-156K | 104K-208K |

**Limitations of Level 1:**
- Does NOT show companion voucher availability (separate "I class" inventory)
- May have slight delays vs real-time BA data (cached)
- Does not show partner airline availability (only BA metal flights)
- Cannot book — only shows availability

### Level 2 — Companion Voucher & Verification via ba.com (LOGIN REQUIRED)

**Use for:** verifying top options from Level 1, AND checking companion voucher
availability (2-for-1 Avios). Only use for specific routes/dates the user is
interested in — minimize requests to avoid anti-bot triggers.

**IMPORTANT:** Only proceed to Level 2 when the user explicitly asks to verify
specific options, or requests companion voucher search. Do NOT auto-verify all
Level 1 results — it's slow and may trigger BA's anti-bot.

**Tool:** Camoufox browser (ba.com uses Akamai anti-bot)

**Prerequisites:**
- BA Executive Club login credentials (stored as facts: `ba_exec_club_email`, `ba_exec_club_password`)
- If credentials not stored, ask user to provide them (store securely via FACTS_UPDATE)

**Workflow:**
1. Create fresh Camoufox tab
2. Navigate to: `https://www.britishairways.com/travel/redeem/execclub/_gf/en_gb`
3. Log in with Executive Club credentials (if not already logged in via saved cookies)
4. Select "Book reward flights" or "Spending Avios"
5. Enter route: origin → destination
6. Enter dates (from Level 1 results)
7. Select cabin class
8. **For companion voucher:** tick "Use companion voucher" / "2-4-1 voucher" checkbox
9. Search and parse results:
   - Available flights with Avios cost
   - Taxes and fees (cash portion)
   - Companion voucher eligible flights (marked separately)
10. Present: flight options with Avios cost, taxes, and companion voucher status

**Companion voucher key facts:**
- 2-for-1: pay Avios for one seat, get second free (still pay taxes on both)
- Only valid on BA-operated flights (not partner airlines)
- Works on reward flights booked entirely with Avios (not Avios + cash)
- Available in all classes: Economy, Premium Economy, Business, First
- Must be booked online via ba.com (not via phone or travel agent)
- Voucher comes from BA Amex card (Premium Plus or regular after spend target)
- Check voucher expiry date — stored as fact `ba_companion_voucher_expiry`

**Anti-bot precautions for ba.com:**
- Use Camoufox (NOT Playwright) — ba.com uses Akamai Bot Manager
- Maximum 5-10 searches per session to avoid triggering blocks
- Add 3-5 second delays between searches
- If blocked, wait 30 minutes and try with fresh tab/cookies
- Consider residential proxy if datacenter IP is consistently blocked

**Cost comparison with companion voucher:**
When presenting results, ALWAYS calculate the value:
```
Without voucher: 2 × Avios + 2 × taxes
With voucher:    1 × Avios + 2 × taxes
Saving:          1 × Avios value (at ~1p/Avios: saving = Avios ÷ 100 in GBP)
```

### Combined Flight Search with BA Avios

When user asks about flights AND mentions Avios/rewards/companion voucher:
1. **Parallel:** Run standard flight search (Kiwi + Google) for cash prices
2. **Parallel:** Run Level 1 Avios scan for same route
3. Present BOTH: cash price vs Avios cost (calculate pence-per-point value)
4. If Avios option looks good → offer Level 2 verification for companion voucher
5. Show total comparison:
   ```
   💷 Cash: £450 return (Google Flights)
   🎯 Avios: 52,000 + £350 taxes (Level 1 scan)
   🎯 Avios + companion: 26,000 + £350 taxes (if Level 2 confirms)
   💡 Value: 0.38p/Avios without CV, 0.19p/Avios with CV
   ```

---

## Hotel Search Workflow — Google Hotels via Playwright

### Step 1: Navigate to Google Hotels
```
URL format:
https://www.google.com/travel/hotels?q={destination}&dates={checkin},{checkout}&adults={n}&currency={currency}

Examples:
https://www.google.com/travel/hotels?q=Istanbul+hotels&dates=2026-04-03,2026-04-10&adults=2&currency=GBP
https://www.google.com/travel/hotels?q=hotels+in+Chiswick+London&dates=2026-03-20,2026-03-22&adults=1&currency=GBP
```

### Step 2: Handle cookie consent
Google may show a cookie consent dialog. Click "Accept all" if it appears.

### Step 3: Extract hotel results
Use `browser_snapshot` to get the accessibility tree, then parse:
- Hotel names
- Star ratings
- Price per night (aggregated across OTAs)
- Review scores
- Key amenities (pool, parking, breakfast, etc.)
- Booking source links

### Step 4: For detailed hotel info
Click on a hotel to see:
- All OTA prices side-by-side (Booking, Expedia, Hotels.com, official site)
- Room types and availability
- Photos, location on map
- Full review breakdown

### Step 5: Present results
Format as a comparison table with:
- Hotel name + star rating
- Price range (cheapest OTA)
- Review score
- Key amenities
- Best booking link

### Google Hotels coverage:
- Aggregates from: Booking.com, Expedia, Hotels.com, Agoda, Trip.com, Priceline,
  official hotel websites, and 20+ other OTAs
- Covers: worldwide, from budget hostels to luxury resorts
- Shows: price comparison across all sources for the same hotel
- Missing: some boutique/local hotels not on major OTAs, Airbnb alternatives

### Google Hotels URL parameters:
| Param | Example | Description |
|-------|---------|-------------|
| `q` | `Istanbul+hotels` | Destination query |
| `dates` | `2026-04-03,2026-04-10` | Check-in, check-out (YYYY-MM-DD) |
| `adults` | `2` | Number of adults |
| `children` | `1` | Number of children |
| `currency` | `GBP` | Price currency |
| `sort` | `price` or `review` | Sort order |
| `price` | `0,200` | Price range filter (per night) |
| `class` | `3,4,5` | Star rating filter |

### Handling large results:
Google Hotels typically shows 20-30 hotels per page. Use `browser_snapshot` to capture
the first page of results. If user needs more, scroll down or click "Show more".

### Anti-bot considerations:
Google Hotels on google.com does NOT use aggressive anti-bot (no PerimeterX, no CAPTCHA).
Standard Playwright works fine — no need for Camoufox. If cookie consent pops up,
just click "Accept all" and continue.

### Efficient snapshot parsing:
Google Hotels snapshots are HUGE (~180KB). To extract data efficiently:

**Option A — Use `browser_evaluate` to extract structured data directly:**
```javascript
// Run via browser_evaluate after page loads
Array.from(document.querySelectorAll('[data-hotel-id]')).slice(0, 15).map(el => ({
  name: el.querySelector('h2')?.textContent?.trim(),
  price: el.querySelector('[data-price]')?.textContent?.trim() ||
         el.textContent.match(/[€£$]\d+/)?.[0],
  rating: el.textContent.match(/(\d\.\d) out of 5/)?.[1],
  stars: el.textContent.match(/(\d)-star hotel/)?.[1],
}))
```

**Option B — Save snapshot to file and grep:**
```
browser_snapshot(filename="hotels.md")
```
Then use Grep tool to find price patterns (`£\d+`, `€\d+`) in the saved file.

**Option C — Full snapshot + parse in Python:**
Capture full snapshot text, then use Bash with Python to extract structured data.

Prefer Option A when possible — smallest token footprint, direct structured output.

---

## Hotel Review Verification — TripAdvisor + Booking.com (MANDATORY)

After finding candidate hotels via Google Hotels, ALWAYS verify ratings on TripAdvisor
and Booking.com before presenting to the user. This is NOT optional.

### Quality Filters (from stored facts):

Before searching, load filter thresholds from the knowledge graph:
- `hotel_min_stars` → minimum star rating (use in Google Hotels URL: `class=X,5`)
- `hotel_min_booking_score` → minimum Booking.com score (out of 10)
- `hotel_min_tripadvisor_score` → minimum TripAdvisor score (out of 5)
- `hotel_require_pool` → if true, filter for pool amenity
- `hotel_require_breakfast` → if true, prioritize breakfast-included

If no facts are stored, DO NOT filter — show all results and let the user decide.
If the user specifies thresholds in the message, use those (and optionally store as facts).
If a hotel fails ANY active filter, exclude it from results unless explicitly requested.

### Workflow: Parallel Review Lookup

For each candidate hotel (top 5-7 from Google Hotels), look up reviews in PARALLEL:

**Step 1 — TripAdvisor lookup (Playwright):**
```
URL: https://www.google.com/search?q={hotel_name}+{city}+site:tripadvisor.com
```
1. Navigate to Google search with the query above
2. Click the first TripAdvisor result
3. Extract via `browser_evaluate`:
   - Overall rating (out of 5.0)
   - Number of reviews
   - Ranking in city (e.g. "#12 of 234 hotels in Dublin")
   - Category scores if available (Location, Cleanliness, Service, Value)
   - **Review highlights — MANDATORY:**
     a) Top 3 POSITIVE themes from recent reviews (what guests love)
     b) Top 3 NEGATIVE themes from recent reviews (what guests complain about)
     c) **Query-relevant features** — extract specifics matching the team's requirements:
        - Pool: heated? indoor/outdoor? kids pool? temperature? opening hours?
        - Group facilities: meeting rooms? event spaces? group activities?
        - Beach: private? proximity? quality?
        - Food: breakfast quality? restaurant quality? dietary options?
        - Room size: actual m², crib/extra bed experience
        - Any feature the user specifically asked about
4. If TripAdvisor rating < minimum threshold from stored facts → exclude hotel

**Alternative TripAdvisor URL (direct):**
```
https://www.tripadvisor.com/Search?q={hotel_name}+{city}&searchSessionId=...
```
Navigate, click first hotel result, extract rating.

**Step 2 — Booking.com lookup (Playwright):**
```
URL: https://www.booking.com/searchresults.html?ss={hotel_name}+{city}&checkin={date}&checkout={date}
```
1. Navigate to Booking.com search
2. Click the specific hotel from results
3. Extract via `browser_evaluate`:
   - Overall score (out of 10.0)
   - Number of reviews
   - **Star rating** (official hotel classification — 3★, 4★, 5★)
   - Category scores (Cleanliness, Comfort, Location, Facilities, Staff, Value)
   - **Review highlights — MANDATORY:**
     a) "Guests loved" / top positive highlights
     b) "Could be better" / negative highlights
     c) **Query-relevant details** from reviews:
        - Specific amenity experiences (pool temperature, breakfast variety, etc.)
        - Group-friendliness mentions
        - Noise levels, room condition, staff helpfulness
   - Key amenities list (pool type, parking, breakfast, spa, gym, etc.)
4. If Booking.com score < minimum threshold from stored facts → exclude hotel

**Step 3 — Entravel price comparison:**
Entravel is a members-only discount OTA with negotiated rates, often 15-40% cheaper
than Booking.com. Login credentials stored in facts (`entravel_email`, `entravel_password`).

**⚠️ IMPORTANT: See "Entravel Hotel Search — Pitfalls & Working Approach" section above
for critical SPA caching issues. The workflow below assumes you follow those rules.**

**Search workflow:**
1. Navigate to `https://entravel.com/hotel/{hotel_id}` (get ID from search results)
2. On hotel page: use **date picker UI** to set dates (DO NOT rely on URL params)
3. Set guests to "1 Room / 2 Adults" (NEVER include children — causes 0 rooms)
4. Click "Search" and wait for rooms to load

**Room type & price extraction (per hotel):**
1. On the hotel page, extract ALL available room types with prices
2. Look for team-suitable room categories — common hierarchy:
   - Standard / Deluxe (may not fit group)
   - Premium / Premium View (check if extra bed available)
   - Junior Suite / Suite (often allows extra bed)
   - Group Room / Family View (purpose-built for groups)
3. For each room type, note:
   - Room name and size (sqm if shown)
   - Entravel price per night (in displayed currency — EUR/GBP/USD)
   - Whether extra bed / crib is available (indicated in room name)
   - Cancellation policy (free cancellation date if applicable)
4. Convert prices to GBP for comparison if not already in GBP

**Price comparison methodology:**
When comparing Entravel vs Booking.com:
- Compare Entravel price vs Booking.com **refundable** rate (like-for-like)
- Also note Booking.com non-refundable rate for reference
- Calculate savings: `(Booking_refundable - Entravel) / Booking_refundable * 100`
- Present as: "Entravel: £X/night (saves Y% vs Booking refundable £Z/night)"

**When to use Entravel:**
- ALWAYS check Entravel as a price comparison source alongside Google Hotels and Booking.com
- Especially valuable for luxury/5-star resorts and all-inclusive properties
- Prices are in USD — convert to GBP for comparison
- Not all hotels are on Entravel — only report those that are found
- If Entravel returns 0 rooms: (1) verify no children in params, (2) use date picker UI
  to re-set dates and click Search — see "Entravel Pitfalls" section for full workflow

**Anti-bot notes:**
- Both TripAdvisor and Booking.com work with Playwright (no Camoufox needed usually)
- If blocked on Booking.com, switch to Camoufox
- If TripAdvisor shows CAPTCHA, use Google search → click through approach
- Entravel works with both Playwright and Camoufox (see Entravel Pitfalls section for caching issues)
- Rate limit: max 5-8 lookups per session, add 2-3s delay between requests

### Efficient Batch Lookup

To minimize tool calls, batch the lookups:
1. Get hotel list from Google Hotels (Step 1-3 of Hotel Search)
2. For each shortlisted hotel, check FOUR price sources in parallel:
   - Tab 1: Booking.com search (ratings + prices)
   - Tab 2: Entravel hotel page (discounted prices — use date picker workflow)
   - Tab 3: Expedia TAAP hotel page (agent net prices)
   - Tab 4: TripAdvisor search (ratings only)
3. Extract ratings from Booking + TA, prices from Booking + Entravel + TAAP
4. Convert ALL prices to GBP using live exchange rates (see Currency Conversion section)
5. Filter: keep only hotels meeting quality thresholds from stored facts
6. Present filtered results with ratings from all sources + best price across OTAs

### Presentation Format — MANDATORY for every hotel in results:

Each hotel MUST include ALL of the following sections. Do NOT skip any section.

```
🏨 Hotel Name ⭐⭐⭐⭐⭐ (5-star)
📍 Location: Grand Canal Square, Docklands, Dublin

💰 PRICES (all in GBP, [room type] / [N] nights):
   Booking.com:  £1,658 (€1,969 @ 0.842) — non-refundable
   Entravel:     £1,815 (€2,156 @ 0.842) — Price Guarantee
   TAAP net:     £1,820 ($2,300 @ 0.791) — agent rate after commission
   Google:       £1,700 (aggregated)
   ✅ CHEAPEST: Booking.com — £1,658

📊 RATINGS:
   Booking.com: 8.5/10 (1,234 reviews) — Exceptional
   TripAdvisor: 4.3/5 (2,345 reviews) — #12 of 234 hotels in Dublin

🏊 KEY AMENITIES:
   Pool: Indoor heated 25m + kids splash pool (28°C year-round)
   Spa | Gym 24h | Restaurant | Bar | Room service
   Parking: €20/day | WiFi: Free | Breakfast: €35/person

✅ WHAT GUESTS LOVE (from reviews):
   • "Amazing rooftop bar with city views"
   • "Breakfast is outstanding — huge buffet selection"
   • "Staff went above and beyond for our kids"

⚠️ WHAT TO WATCH OUT FOR (from reviews):
   • "Standard rooms are small for a 5-star"
   • "Pool area gets crowded on weekends"
   • "Parking is expensive and fills up fast"

🎯 RELEVANCE TO YOUR TRIP:
   • Pool: ✅ Heated indoor — good for the team in April
   • Group rooms: ✅ 40m², extra bed available
   • Location: 10 min walk to city center, steps from LUAS tram
   • [Any other feature the user asked about]
```

**RULES for presentation:**
- Star rating = official hotel classification (from Booking.com), show as ⭐ symbols
- Ratings from BOTH Booking.com AND TripAdvisor — never skip one
- Review highlights: minimum 3 positive + 2 negative from actual guest reviews
- Amenity details: be SPECIFIC (not just "pool" but "indoor heated 25m pool")
- Relevance section: match hotel features against the user's specific requirements
  (e.g. if they asked about heated pools, report pool temperature and type)
- Price comparison: always show all 4 sources, always highlight cheapest
- If a source doesn't have the hotel: show "N/A" (don't silently omit)
Always highlight the cheapest source. Show original currency + GBP conversion + rate used.

### When to Skip Review Verification:
- User explicitly says "don't check reviews" or "just show prices"
- Time-critical search where user needs quick results (ask first)
- Hotels the team has already stayed at (check stored facts)

### Stored Preferences (knowledge graph):
Before every hotel search, check stored facts for preferences:
- `hotel_min_stars` → star rating filter
- `hotel_min_booking_score` → Booking.com minimum score
- `hotel_min_tripadvisor_score` → TripAdvisor minimum score
- `hotel_require_pool` → pool filter
- `hotel_require_breakfast` → breakfast filter
- Any other hotel_* facts → apply as filters
These facts are managed by the user via FACTS_UPDATE — never hardcoded in the skill.

---

## Car Hire Search Workflow — Google Cars via Playwright

### Step 1: Navigate to Google Car Rental
```
URL format:
https://www.google.com/travel/cars?q={destination}&dates={pickup},{return}&currency={currency}

For airport-specific pickup:
https://www.google.com/travel/cars?q=car+rental+{airport_code}&pickup={datetime}&return={datetime}&currency={currency}

Examples:
https://www.google.com/travel/cars?q=car+rental+San+Diego&dates=2026-04-03,2026-04-10&currency=GBP
https://www.google.com/travel/cars?q=car+rental+SAN+airport&dates=2026-04-03,2026-04-10&currency=GBP
```

### Step 2: Extract car results
Use `browser_snapshot` or `browser_evaluate` to parse:
- Car category (Economy, Compact, SUV, etc.)
- Car model / example
- Price (total and per day)
- Rental company (Hertz, Avis, Enterprise, etc.)
- Pickup/return location
- Key features (AC, automatic, luggage capacity)
- Booking link

### Step 3: Present results
Format as comparison with:
- Car type + example model
- Price total for trip (and per day)
- Rental company
- Key features
- Booking link

### Google Cars coverage:
- Aggregates from: major rental companies (Hertz, Avis, Enterprise, Budget, Sixt,
  Europcar, National, Alamo) + aggregators (Rentalcars.com, Kayak, etc.)
- Worldwide coverage, especially strong at airports
- Shows price comparison across providers for same car category

### Anti-bot considerations:
Same as Google Hotels — standard Playwright works, no CAPTCHA. Cookie consent may appear.

---

## Trip Planning Workflow — Combined Search

When user asks for a full trip (flights + hotels, or flights + hotels + car):

### Step 1: Search all components in parallel
Use Agent tool to spawn parallel searches:
- Agent 1: Flights via Kiwi + Google Flights MCP
- Agent 2: Hotels via Google Hotels Playwright
- Agent 3: Car hire via Google Cars Playwright (if needed)

### Step 2: Combine and optimize
- Match hotel dates to flight arrival/departure
- For car hire: match pickup to flight arrival, return to flight departure
- Calculate total trip cost (flights + accommodation + transport)
- Consider location: hotel near airport for early/late flights, central for tourism

### Step 3: Present trip summary
```
✈️ FLIGHTS
  Outbound: [date] [airline] [route] — £XXX
  Return: [date] [airline] [route] — £XXX

🏨 HOTEL ([N] nights)
  [Hotel name] ⭐[rating] — £XXX/night (£XXX total)
  via [OTA] | [key amenities]

🚗 CAR HIRE ([N] days) — if applicable
  [Car type] via [company] — £XXX total
  Pickup: [location] | Return: [location]

💰 TOTAL ESTIMATED COST: £X,XXX
```

### Budget optimization tips:
- Search flexible dates (±3 days) — can save 30-50% on flights
- Mid-week departures are typically cheaper
- Hotels booked 2-4 weeks ahead often have best rates
- Car hire from airport is usually cheaper than city center pickup
- Consider nearby airports (e.g. LGW vs LHR, SAW vs IST) for savings

---

## Presentation Guidelines

### For flights:
- Always show: price (GBP), departure/arrival times with timezone, duration, stops, airline
- For layovers: show airport, duration, terminal if available
- Prefer daytime flights when possible (check stored facts for user preferences)
- Include booking link or redirect URL
- If comparing sources: show price from each source side by side

### For hotels:
- Always show: name, stars, price/night, total price, key amenities
- **MANDATORY: show ratings from ALL THREE sources** — Google, Booking.com (X/10), TripAdvisor (X/5)
- Apply quality filters from stored facts (hotel_min_stars, hotel_min_booking_score, hotel_min_tripadvisor_score)
- Show cheapest booking source for each hotel
- Note if breakfast/parking included
- Highlight suitable features (pool, kids activities, team/group rooms)
- Include 1-2 "guests loved" highlights and 1 "watch out" from reviews

### General:
- Currency: default GBP unless user specifies otherwise
- Language: match user's language (Russian/English)
- Always run multiple sources in parallel when possible
- Present top 5-7 options unless user asks for more
- Include links for all recommended options

---

## Common IATA Codes:
- London: LHR (Heathrow), LGW (Gatwick), STN (Stansted), LTN (Luton), LCY (City)
- Moscow: SVO (Sheremetyevo), DME (Domodedovo), VKO (Vnukovo)
- Istanbul: IST (new airport), SAW (Sabiha Gökçen)
- Dubai: DXB
- Doha: DOH
- US West: LAX (Los Angeles), SFO (San Francisco), SAN (San Diego), SJC (San Jose), SEA (Seattle), LAS (Las Vegas), PHX (Phoenix)
- US East: JFK/EWR/LGA (New York), MIA (Miami), ORD (Chicago), BOS (Boston), IAD/DCA (Washington)
- Europe: CDG/ORY (Paris), FCO (Rome), BCN (Barcelona), AMS (Amsterdam), FRA (Frankfurt), ZRH (Zurich), VIE (Vienna), ATH (Athens)
- Asia: BKK (Bangkok), SIN (Singapore), HKG (Hong Kong), NRT/HND (Tokyo), ICN (Seoul)
- Resort: CUN (Cancun), PUJ (Punta Cana), MLE (Maldives), HER (Crete), PMI (Mallorca)

## Cost & Limits Summary:
| Source | Cost | Rate Limit | API Key |
|--------|------|-----------|---------|
| Kiwi MCP | Free | Unlimited* | None |
| Google Flights (fli) | Free | 10 req/s | None |
| Google Hotels (Playwright) | Free | ~10 req/s | None |
| Google Cars (Playwright) | Free | ~10 req/s | None |
| Aviasales (Camoufox) | Free | ~1 req/5s | None |
| BA Avios L1 (AwardTravelFinder) | Free | ~10 req/s | None |
| BA Avios L2 (ba.com Camoufox) | Free | 5-10/session | BA Exec Club login |
| Camoufox (fallback) | Free | Manual | None |
| TripAdvisor (Playwright) | Free | ~5-8/session | None |
| Booking.com (Playwright) | Free | ~5-8/session | None |
| Entravel (Playwright) | Free | Login required | entravel_email/password in facts |
| Expedia TAAP (Playwright) | Free | Login required | expedia_taap_email/password in facts |

*Kiwi may introduce limits in future — monitor.
All sources are reverse-engineered or unofficial except Kiwi MCP (official).
Google-based tools risk breakage if Google changes internal APIs.
Entravel requires login credentials stored in facts (entravel_email, entravel_password).
Expedia TAAP requires agent login stored in facts (expedia_taap_email, expedia_taap_password).
TAAP account is via Fora Travel agency (Platinum tier) — use agent net prices for comparison.

---

## 🎙 TTS Audioguides — Gemini Voice Generation

Generate audio guides for any city, neighborhood, or landmark on the trip.
Uses **Gemini 2.5 Flash TTS** (`gemini-2.5-flash-preview-tts`).

### When to Use
- User asks for an audioguide, audio tour, or voice overview of a location
- User says "сгенерируй аудиогид", "расскажи голосом", "пришли аудио про..."
- Proactively offer for each new city/neighborhood on the walking route

### How to Generate

1. **Write a Python script** to `/app/data/tmp/tts_<topic>.py`:

```python
import sys, wave
sys.path.insert(0, "/app")
from config import GEMINI_API_KEY
from google import genai
from google.genai import types

client = genai.Client(api_key=GEMINI_API_KEY)

text = """<RUSSIAN TEXT — 1-3 minutes of narration.
Write numbers as words (двадцать два миллиона, не 22 млн).
Use conversational tone, like a knowledgeable friend, not a textbook.
Include: founding history, key facts, what to notice, local tips.>"""

response = client.models.generate_content(
    model="gemini-2.5-flash-preview-tts",
    contents=text,
    config=types.GenerateContentConfig(
        response_modalities=["audio"],
        speech_config=types.SpeechConfig(
            language_code="ru-RU",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="charon"
                )
            ),
        ),
    ),
)

audio = response.candidates[0].content.parts[0].inline_data
wav_path = "/app/data/tmp/audioguide_<topic>.wav"
with wave.open(wav_path, "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(24000)
    wf.writeframes(audio.data)
print(f"OK: {wav_path} ({len(audio.data)} bytes)")
```

2. **Run**: `python3 /app/data/tmp/tts_<topic>.py` (timeout 120s)
3. **Send**: `telegram_send_audio` with the WAV file

### Important Notes
- **NEVER use `os.environ`** in bash commands (blocked by security). Import `GEMINI_API_KEY` from `config` module instead.
- Audio output is **raw PCM L16 24kHz mono** — wrap in WAV header via `wave` module.
- Voice options: `charon` (deep male), `puck` (energetic), `kore` (female), `leda` (female).
- `language_code="ru-RU"` for Russian, `"en-US"` for English, `"pt-BR"` for Portuguese.
- Each audio is ~2 min narration ≈ 3-6 MB WAV.
- Generate multiple audioguides in parallel (separate scripts) for efficiency.
- Text should spell out numbers, abbreviations, and foreign names phonetically for natural speech.

---

# Known Gotchas (Real Failures → Rules)

Observed production failure modes. Learn them; don't re-discover them.

## ⛔ Google Flights (fli) rate-limited at 10 req/s
**Symptom**: HTTP 429 from `google_flights_search` when parallelizing too aggressively.
**Fix**: Concurrency capped at 8 via `_GFLIGHTS_SEM` semaphore (Google 429s at ~9 concurrent).
Rate limit backoff + retry is built in. Don't bypass the semaphore.

## ⛔ Marriott.com scraping is IMPOSSIBLE via Playwright
**Symptom**: Akamai WAF challenge on every request to marriott.com.
**Rule**: Use Google Hotels (aggregates Marriott rates from OTAs) for rate comparison.
Use TAAP for agent rates. Direct-email Marriott for Bonvoy perks/special requests.
NEVER try to scrape marriott.com directly — you'll just ban the IP.

## ⛔ TAAP flight-only bookings are non-commissionable
**Symptom**: Submitted Fora bookings for TAAP flight-only trips get REJECTED.
**Rule**: Don't submit TAAP flight-only to Fora — policy says flight-only bookings via
consolidators earn no commission. Hotel + car + package bookings ARE commissionable.

## ⛔ TAAP package-rate hack unlocks 15-30% cheaper hotels
**Rule**: Always search TAAP with 'Package rates' tab + add cheapest 1-day car rental to
unlock package hotel rates. Skip only when already booking flight+hotel package.

## ⛔ Camoufox session profiles must be loaded BEFORE searches
**Symptom**: TAAP/Fora re-prompt login every session; searches fail.
**Fix**: `load_profile('taap_session')` or `load_profile('fora_session')` FIRST. Profiles
are saved with 556+ cookies (TAAP) / 598 cookies (Fora) per recent snapshots.

## ⛔ Flight preferences must cover BOTH parents
**Rule**: When flying Parent1 + Parent2 together: same flight, same cabin class. Match Parent2's
class (typically Norse Premium / PE). Never split onto different flights/airports.
