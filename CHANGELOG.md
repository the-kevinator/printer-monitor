# Changelog

## 1.5.0 — 2026-10-15

The version jumps from 1.1.0 straight to 1.5.0; there were no 1.2–1.4
releases. Everything below has landed since 1.1.0.

### At a glance

- **Export** the whole inventory, notes included, as **CSV** or **PDF**.
- **Dark mode**, with a toggle in the header of every page.
- **Colour tags** for printers, and a **name for each colour** ("Blue: 2nd
  floor") that follows it everywhere.
- **Accounts panel** in the dashboard, and **sorting from the toolbar** in
  both the card and table views.
- Fixed: notes and other edits on a printer's own page failed with
  "Forbidden"; after an upgrade, browsers could show new pages with old styles.
- Still three dependencies. Nothing new to install.

### New

#### Export to CSV or PDF

An **Export** menu sits at the right of the toolbar, beside Cards/Table.

- **CSV** — one row per printer with every field: name, address, hostname,
  sysName, vendor, model, serial, MAC, location, colour tag and what it means,
  status, online, polling on/off, lowest toner, each cartridge's level, page
  count, last polled / last seen / first seen, web interface, source and
  **notes**. It opens straight into Excel with accented characters intact
  (UTF-8 with a byte-order mark). Any cell that starts with `=`, `+`, `-` or
  `@` gets a leading apostrophe so a spreadsheet shows it as text instead of
  running it as a formula — notes are free text typed into the dashboard, and
  that is the standard CSV-injection route.
- **PDF** — a landscape US Letter inventory: printer, address, model,
  location, status, lowest toner, pages and notes, with the colour swatch
  beside each name. The header carries a count by status, the export time and
  a legend for the colour tags in use. Column headings repeat and pages are
  numbered "Page n of N". It is written by the app itself on the standard
  library (`app/export.py`), so there is no PDF package to install. A row
  cannot continue onto the next page, so a single note longer than about a
  page is cut short with "continued in the CSV export"; the CSV always has it
  in full.

Files download as `printers-YYYY-MM-DD.csv` / `.pdf`. Exports are open to
anyone who can view the dashboard, like every other read: they contain what
`/api/printers` already serves.

#### Dark mode

A sun/moon button in the header switches between light and dark on the
dashboard and the printer page; the sign-in page follows the same choice.
Until you press it the app follows the operating system's setting. The choice
is remembered per browser. The theme is applied before the page draws, so a
dark page never flashes white while loading. Printing always comes out on a
white background whatever the screen is set to.

#### Colour tags

Give any printer one of nine colours — red, orange, yellow, green, teal,
blue, purple, pink, gray — from **Settings for this printer** on its own
page. A tagged printer shows:

- a coloured band across the top of its card,
- a dot beside its name on the card, in the table and on its own page.

A **colour filter** in the toolbar shows one colour at a time. It lists only
colours in use, and stays hidden until something is tagged. Tags are separate
from status: the Ready / Attention / Critical / Offline colours and icons are
unchanged. Setting a tag is an edit, so it needs a sign-in like any other
change.

#### Naming the colours

**Tags** in the dashboard toolbar (also **Name the colors** above the picker
on a printer's page) opens one row per colour with a box to say what it
means — a floor, a department, who looks after it — and how many printers
use it. Up to 40 characters; clearing a box removes the name.

The name belongs to the colour, not a printer, so every printer with that
colour picks it up. It appears:

- on the picker, in place of the bare colour name,
- in the colour filter and in tooltips, as "Blue: 2nd floor",
- in the CSV, as a **Color means** column,
- in the PDF, in the legend under the title.

Names are kept in `data/tags.json`, created the first time you save one.
Reading them is open; saving needs a sign-in.

#### Accounts panel

**Accounts** in the toolbar (shown only when signed in) lists who can make
changes and when each last signed in, adds and removes accounts, and changes
your own password. The last account cannot be removed, since that would open
the dashboard to everyone again; removing your own account signs you out. To
reset someone else's password, use `manage.py user passwd <name>` on the
server and restart the service.

#### Sorting from the toolbar

A **Sort** dropdown and direction button in the toolbar sort by name,
address, model, status, toner, page count, location or last poll — in the
card view as well as the table. Clicking a table header still works and keeps
the two in step. The choice is remembered in the browser.

### Fixed

- **Edits on a printer's own page failed with "Could not save: Forbidden".**
  Saving notes, polling one printer and Forget all omitted the security
  header every change must send. They now send it, show the server's own
  error message, and send you to sign in if your session has expired.
- **After an upgrade, a browser could show the new pages with the old
  stylesheet.** This is how the colour picker first appeared as a row of
  identical radio buttons. Stylesheet and script links now carry a
  fingerprint of the file's contents (`style.css?v=…`), so every upgrade is
  fetched fresh. The colour picker also names each option in text, so it
  stays readable even without colour.

### Changed

- **Windows-service install is now the recommended deployment** in the
  README, using NSSM, instead of IIS. The poller runs inside the app, which
  IIS app-pool recycling works against. The IIS route is still documented.
  The README also covers the two traps of a fresh install: `data/` and
  `logs/` are not in git and must exist, and `manage.py serve` listens on
  `127.0.0.1` unless given `--host 0.0.0.0`.
- **README screenshots** are now taken from an invented demo fleet
  (`tools/demo_fleet.py` + `tools/screenshots.py`), and the sample account in
  tests and help text is `jdoe`.

### API

| Method | Path | |
|---|---|---|
| `GET` | `/export/printers.csv` | New. CSV download |
| `GET` | `/export/printers.pdf` | New. PDF download |
| `GET` | `/api/tags` | New. The nine colours, each with `name`, `hex`, `label` |
| `PUT` 🔒 | `/api/tags` | New. `{"labels": {"blue": "2nd floor"}}`; `""` clears, colours left out are unchanged |
| `PATCH` 🔒 | `/api/printers/{ip}` | Now also takes `color`: one of the nine names, or `""` to clear. Anything else is a 400 |
| `GET` | `/api/printers` | Each printer now has `color`; the response adds `tags` |

### Upgrading from 1.1.0

1. On the server, pull the new version into the app folder.
2. Restart the service: `Restart-Service PrinterMonitor`.

No new packages, no config changes and nothing to migrate. Existing printers
start untagged; `printers.json` gains a `color` field as printers are saved,
and `data/tags.json` appears the first time a colour is named. Going back to
1.1.0 still runs, but it drops the `color` field the next time it saves the
inventory, so the tags would have to be set again.

Tests: 223, up from 203 at 1.1.0 (`.venv/bin/python -m pytest`).
