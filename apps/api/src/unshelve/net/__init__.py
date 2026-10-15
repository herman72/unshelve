"""The only code that opens a connection to the web.

- ``fetcher.LiveFetcher`` and its guard open a saved link (ADR 0010).
- ``service.ServiceClient`` talks to one fixed service, such as the API of Voyage (ADR 0012).
"""
