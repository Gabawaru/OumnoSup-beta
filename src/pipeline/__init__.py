"""Data pipeline.

Mandatory flow for every record, without exception::

    scrape -> parse -> normalize -> validate -> deduplicate -> store

No raw scraped payload is ever written straight to the database.
"""
