# arXiv fixtures

Made-up values in the Atom form of the real answers of the API of arXiv
(`export.arxiv.org/api/query?id_list=…`), as they were on 15 October 2026. Titles and abstracts
keep the line breaks that the API puts into them. No text of a real paper is here.

`error.xml` has the form of an error entry in the manual of the API (section 3.4, "Errors"). On
15 October 2026 the real API answered an identifier in a wrong form with an empty feed instead.

`tests/extract/test_arxiv_live.py` checks the form of real answers (`-m live`).
