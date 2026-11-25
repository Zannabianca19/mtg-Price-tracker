# MTG price tracker for Cardmarket

A free web app to follow Cardmarket trend prices of Magic: The Gathering cards: any printing, normal or foil, whole sets, Commander and Duel Commander decks, with 1/7/30-day changes, market movers and price alerts. Available in English, Italian, French, Spanish and German.

- `site/` – the web app (a single `index.html`, plus preview image and robots.txt).
- `scripts/build.py` – daily job: downloads Scryfall's `default_cards` bulk file, appends today's EUR and EUR-foil prices to the archive, and computes the market movers.
- `.github/workflows/archivio.yml` – runs the job every day and publishes site + data on GitHub Pages.

Prices: Cardmarket trend price as published by [Scryfall](https://scryfall.com), updated once a day. This project is not affiliated with Cardmarket or Scryfall.
