"""
encar -- One crawler for Encar listings, one database for what it finds.

Every route this package knows (the JSON API direct or through a proxy service,
a local browser, the hosted Bright Data collector) reads the same thing: the
listings Encar advertises for a model, and the vehicle number behind each one.
They differ only in how they get there. So they all produce the same row shape
(see parse.py), and every run lands in the same SQLite file (see store.py),
which is also what the dashboard reads and what sold detection works from.

    python main.py crawl                 # every model in models.json
    python main.py crawl --model bmw_3series_g20 --pages 1-2 --no-sold
    python main.py export --model bmw_3series_g20
    python main.py check-plate 110모5877
    python main.py import-stock
    python main.py dashboard
"""
