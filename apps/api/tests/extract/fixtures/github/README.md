# GitHub fixtures

Made-up values in the structure of the real answers of the REST API of GitHub, as they were on
15 October 2026: `GET /repos/{owner}/{repo}` and `GET /repos/{owner}/{repo}/readme` (base64, with a
line break after each 60 characters). No text of a real repository is here.

`tests/extract/test_github_live.py` checks the structure of real answers (`-m live`).
