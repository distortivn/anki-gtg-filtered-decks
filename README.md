# GTG Filtered Deck Creator

An Anki add-on that turns one search query into a full day's worth of filtered decks, split by the hour.

Instead of sitting in front of one giant deck of 2,000 cards, you get a tree of small decks laid out across your day. Each one is a bite: finish it, close it, move to the next.

```
日本語語彙
└── Day 3
    ├── 06:00-07:00
    │   ├── 1     (15 cards)
    │   ├── 2     (15 cards)
    │   └── 3     (15 cards)
    ├── 07:00-08:00
    │   ├── 1
    │   ├── 2
    │   └── 3
    └── ...
```

Deck names are zero-padded 24h, so they sort in the right order in the deck list and the browser.

## What it does

- **Splits a query across a time window.** Pick a search (`deck:"X" is:learn`), a start and end hour, how many cards per hour, and how many decks each hour is cut into. The add-on builds every filtered deck and fills it with a fixed slice of the matched cards.
- **Courses.** Tell it a total card count and how many hours a day you study, and it works out how many days the course takes. It tracks which day you're on and names the next container for you (Day 4, Stage 4, Sprint 4 — the word is yours).
- **Quick Create.** `Ctrl+Alt+Q` repeats your last setup with the day number bumped. One keystroke, next day built.
- **Refill.** Point at an existing day and repack every deck in it from a fresh query, keeping the same structure.
- **Empty / Delete.** Return the cards to their home decks, remove the day, or both.

Filtered decks are created with rescheduling on, so reviews count normally. Emptying or deleting a day sends every card back where it came from with its scheduling intact.

## Install

**From the release (easiest)**

1. Download the `.ankiaddon` file from the [Releases](../../releases) page.
2. In Anki: **Tools → Add-ons → Install from file…** and pick it.
3. Restart Anki.

**From source**

Clone into your Anki add-ons folder:

```
# Windows
%APPDATA%\Anki2\addons21\gtg_filtered_decks

# macOS
~/Library/Application Support/Anki2/addons21/gtg_filtered_decks

# Linux
~/.local/share/Anki2/addons21/gtg_filtered_decks
```

Restart Anki. Everything lives under **Tools → GTG Deck Creator**.

Requires Anki 2.1.50 or newer.

## Using it

**Tools → GTG Deck Creator → Create GTG Filtered Decks**, then five steps:

1. **Pick a filter.** Preset buttons for `is:learn`, `is:due`, `is:new`, or `learn + due`; a deck dropdown; or type any Anki search. A live counter shows how many cards match before you commit.
2. **Start and end hour** (24h). `6` to `18` gives you a twelve-hour window.
3. **Cards per hour.** 45 is a normal setting.
4. **Decks per hour.** 3 splits those 45 into three decks of 15.
5. **Parent deck and day name.** Either a one-off name, or a course that names and counts the days for you.

That's `12 × 3 = 36` filtered decks holding 540 cards, and the same number sitting on your shelf tomorrow.

### The other menu items

| Item | What it does |
|---|---|
| ⚡ Quick Create (`Ctrl+Alt+Q`) | Rebuilds yesterday's setup with the day advanced. Skips every prompt. |
| Refill GTG Day | Keeps the deck tree, replaces the cards inside it from a new query. |
| Manage Courses | Rewind a course's day counter, or delete a course. |
| Empty GTG Day | Cards go home, decks stay. |
| Delete GTG Day | Decks go, cards were already returned (or you don't care). |
| Empty + Delete GTG Day | Both, in the right order. |

Creating a day that already exists wipes and rebuilds it, so re-running is always safe.

## Settings

Your last setup and your courses are saved to Anki's own add-on config (`meta.json`). Nothing else is written, nothing leaves your machine.

## License

MIT. See [LICENSE](LICENSE).
