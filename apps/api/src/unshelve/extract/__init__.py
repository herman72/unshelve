"""The extractors: each one reads one kind of source into text, through ``ctx.fetcher``.

An extractor never opens a connection itself (ADR 0010). ``match`` says how well it fits a
source: 0 means "not mine", an extractor for one site (YouTube, GitHub) gives 100, and the
article extractor gives 10 to any web page. The highest score wins.
"""
