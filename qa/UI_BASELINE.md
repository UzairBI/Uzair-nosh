# Nosh Assistant - frontend baseline (FROZEN)

Recorded **before any QA change**. The frontend is finalized by its owner; this QA pass may change **backend only**.
`nosh/static/` is protected by a checksum guard: `python qa/freeze_check.py` (baseline in `qa/baseline/frontend_hashes.json`, 26 files,
`index.html` sha256 starts `a5b4db1d138c55348ee7`, frozen copy in `qa/baseline/index.html.frozen`).
Automated regression suite for everything below: `node qa/ui_regression.js` (drives the real page in headless Edge). Screenshots: `qa/shots/before/`.

## Layout
| Area | Behaviour |
|---|---|
| Sidebar (214 px, dark `#1c1a18`) | NOSH wordmark logo; orange **New chat** button; **My Nosh** (external link to letsnosh.io, new tab); **Help & Support**; **History** accordion; at the bottom the owner's photo + name **Uzair Meran Khan** (no arrow beside it) |
| Main column | Single centred column, max width 820 px, cream background `#fdf4ee`; **no right-hand panel** |
| Header | "Nosh Assistant", orange dot + "Online", subtitle "Here to make everyday cooking effortless" |
| Chat area | Independently scrolling; the page itself never scrolls; auto-follows streamed text but never scrolls past the first line of a long answer; "↓ Latest" pill appears when scrolled up |
| Composer | One rounded card: attachment tray (when files attached) + paperclip with count badge + text input + orange send button; always pinned at the bottom |
| Sizes | Compact scale (~87%); verified at 1280x720, 1366x768, 1920x1080 with no page overflow |

## Components and interactions
1. **Welcome** - bot bubble "Hello! I'm Nosh. ..." and three chips: *Find a quick recipe*, *Match my ingredients*, *Swap an ingredient*. (*Ask about a document* was removed from the welcome screen at the owner's request; the paperclip, drag-and-drop and the Help page examples still offer documents.)
2. **Sending** - user bubble (peach, right), bot bubble (grey, left, flame avatar), typing dots, token streaming, Markdown rendering (headings, bold, lists with nested bullets, tables, code blocks, citation badges `[n]`, `[SQL]`), recipe ids never shown, send button disabled while busy, second message while busy is ignored.
3. **Recipe results** - short intro text first, then recipe photo cards (3 per row: photo, title, "N steps | N ingredients" or diet tag), then "Photos are illustrative..." note, then follow-up chips *Show me more*, *Make it dairy-free*, *Swap an ingredient*. Clicking a card asks for that recipe; the reply is a recipe page (title, counts, Ingredients, Method, Good to know, dietary note). There is **no** "Show full details" control.
4. **New chat** - clears the conversation, shows the welcome again, stores the first question in History.
5. **History** - accordion; each entry restarts a new chat and replays that question.
6. **My Nosh** - opens https://www.letsnosh.io/ in a new tab.
7. **Help & Support page** - replaces the chat view (sidebar item highlighted): contact cards (phone +91 63663 77918, email hello@euphotic.io, Brookefield address with map link), response-time note (15 days), three troubleshooting/FAQ links (Wi-Fi, App settings, Routine maintenance), warranty (up to 2 years), "What Nosh Assistant can do" example buttons (run in chat), **Back to chat**. New chat also returns to chat.
8. **Attachments** - paperclip / drag-and-drop overlay / paste; file tiles with coloured type badge, size and section/row count, upload progress then "Reading and indexing...", error tile for bad files, remove (x), count badge, placeholder becomes "Ask about <file>..." / "Ask about your N documents...", attachments survive a page reload, chat confirms "I've read <file>" with follow-up chips.
9. **Other** - `?q=` auto-sends a message; favicon (flame); all brand assets served (logo, avatar, 8 dish photos).

## Baseline result (before QA changes)
`node qa/ui_regression.js` -> see `qa/baseline/ui_results_before.json` (35/38 on the first run; the 3 misses were then diagnosed as 1 test false-alarm,
1 backend typography issue (R22) and 1 backend wording issue (R21) - all backend/test-side, **no frontend defect**).
