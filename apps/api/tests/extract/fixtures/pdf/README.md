# PDF fixtures

Made-up texts, printed to PDF by a real producer: headless Chrome 155 ("Skia/PDF m155"), on
15 October 2026. No file comes from the web, and no file holds a real person or a path of a
computer. The tests read these bytes as they are: do not open and save them in another
program.

| File | What it is | What it tests |
|---|---|---|
| `paper.pdf` | "Leases for Job Queues in Postgres": 3 pages in two columns, with headings, a link with words ("our data page") and an address in the text | the order of the columns, the facts, the links, the limits of pages and characters |
| `persian.pdf` | One page of Persian with English words, from an HTML page with `dir="rtl"` | glyph forms of the letters, heh (U+0647) that comes out as U+06BE |
| `slides.pdf` | 3 slides of 1280 × 720 points; pypdf 6.20.1 then set the title "Microsoft PowerPoint - queues-talk.pptx" and the author "Made-up Speaker" | a title that a program wrote, an author |
| `scanned.pdf` | One page with a grey picture and no text | a file without a text layer |
| `locked.pdf` | The slides, locked by pypdf with the user password "made-up-password" (AES-256) | a file that needs a password |
| `owner_password.pdf` | The slides with an empty user password and an owner password (AES-128): it opens, and it does not allow printing | a file that only limits printing |
| `cut.pdf` | The first half of the bytes of the slides | a file that is not whole |

Chrome gives the facts of each file: the title from `<title>`, the date of the print as
`CreationDate`, and its own name as `Creator`.

## What Chrome writes for Persian

Chrome draws the glyphs of a right-to-left line in the order of the page, from left to right,
and marks them with `/ReversedChars`. Each glyph has its letter in an `/ActualText` span, and
the font maps it to a glyph form (U+FB50 to U+FEFC). pypdf reads the font map: the words come
out right, in glyph forms that the extractor turns into letters. In a line that mixes Persian
and English, the parts come in the order of the page. With pdfium (pypdfium2 5.14), many words
of the same file came out with their letters in reverse order ("فص" for "صف").
