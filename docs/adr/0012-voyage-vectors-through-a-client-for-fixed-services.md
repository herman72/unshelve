# 0012. Voyage for the vectors, through a client for fixed services

- Status: Accepted
- Date: 2026-10-15

## Context

[ADR 0007](0007-embedding-sets-and-exact-search-first.md) decided the storage of vectors: embedding sets, one card vector for each item, and at most 40 vectors for the pieces of a long text. Until milestone M2-5, a fake embedder made the vectors. The owner chose Voyage AI as the first real embedder ([issue #23](https://github.com/herman72/unshelve/issues/23)).

Three facts shaped the decision:

- **The text is English and Persian.** On the pages that we compared for the model card, a Persian text had 1.4 to 1.6 characters for each token, and an English text 2.6 to 3.4 (counted by the tokenizer of Claude).
- **The Voyage SDK is heavy.** `voyageai` 0.5.0 adds 24 packages that the project does not have now, among them `langchain-core`, `langsmith`, `huggingface-hub`, `tokenizers`, `numpy` and `pillow`. The API that we need is one `POST` request.
- **Only `unshelve.net` opens a connection** ([ADR 0010](0010-one-guarded-way-to-the-internet.md)). A test reads the imports of each module and refuses an HTTP client outside that package.

## Decision

- **The adapter** is `providers/voyage.py` (`VoyageEmbedder`). It sends `POST https://api.voyageai.com/v1/embeddings` with the texts, the model, `input_type` (`document` or `query`) and `output_dimension`. Voyage puts its own short instruction in front of a document and a query, so the embedding set has no prefixes. One request holds at most 128 texts and 100,000 characters: fewer tokens than the limit of `voyage-4-large` (120,000), also for Persian.
- **The address** follows the key, as in the official client: a key of the Voyage platform (`pa-...`) goes to `api.voyageai.com`, a model key of MongoDB Atlas (`al-...`) to `ai.mongodb.com`. Voyage AI is part of MongoDB, and new keys come from Atlas. The two addresses take the same request.
- **The connection** goes through `net/service.py` (`ServiceClient`), a client for one fixed service. Its address is in the code and must be https. A request goes only to a path of that address, never to a whole address. It follows no redirect, reads at most a set number of bytes (counted before and after decompression, as in the fetcher), stops after a set time, and takes no proxy from the environment. The rule of ADR 0010 stays without an exception: only `unshelve.net` imports an HTTP client.
- **The model** is `voyage-4-large` with vectors of 1,024 numbers and cosine distance (`UNSHELVE_EMBED_MODEL`, `UNSHELVE_EMBED_DIM`). Voyage describes it as its model with the best multilingual retrieval.
- **The switch** is its own setting: `UNSHELVE_EMBEDDER=fake|voyage`, with `VOYAGE_API_KEY`. It is not `UNSHELVE_PROVIDERS`, because the first 200 million tokens of Voyage are free for each account, and a call of Claude is paid from the first token. Vectors can be real while the cards stay fake.
- **The pieces** come from `embed/chunks.py`. A text of at most 1,000 characters gets none: its card is enough. A piece has at most 1,500 characters, and ends after a paragraph, else after a sentence or a line, else after a word. A text that needs more than 40 pieces gets longer pieces, up to 4,000 characters. The limits count characters, not tokens: 4,000 Persian characters are about 2,900 tokens, far below the 32,000 tokens that the model reads.
- **The embed step** asks for all new vectors of an item in one request. A vector whose text did not change stays, so a second run makes no request. Before it writes, the step checks that the card and the text of the item are still the ones that it read. Failures fall into the three groups of [ADR 0011](0011-claude-for-the-cards.md). After the last attempt, the item is ready without the new vectors: the vectors of its old text are removed, and an event `embed_failed` says why.
- **A new model** gets a new embedding set, which starts inactive. `unshelve embeddings backfill` adds an embed job for each ready item without a vector in that set. `unshelve embeddings activate` makes it the set that search reads. `unshelve embeddings status` shows the sets, and `unshelve embeddings check` makes one test call.

## Consequences

- No new dependency. The adapter is about 180 lines, and a pretend Voyage in the tests answers in the form of the real API.
- A change of the model is a data operation: a new set, a backfill, then the switch. The old set keeps its vectors; a later command can remove them.
- Search (M4) reads only the active set. Until a new set is active, a new item has vectors only in the new set.
- English pieces hold more words than Persian pieces of the same length in tokens. Both are far below the limits of the model.
- Live tests run only with `-m live`. CI never sends a request to Voyage.
- The numbers of a vector change a little from call to call, because a GPU is not exact: the same text gave a cosine of 0.998 or more, and two different short texts 0.79. A test compares vectors with a tolerance, and the embed step keeps a stored vector instead of making it again.
- A title or a summary that the owner changes by hand (milestone M2-6) must add an embed job, or the card vector stays old.

## Alternatives considered

- **The `voyageai` SDK:** official, but 24 more packages in the worker for one request, each one code that must be updated and trusted.
- **`httpx` directly in `providers/voyage.py`, and an exception in the test of ADR 0010:** a little less code, but the rule "only `unshelve.net` opens a connection" would get its first exception, and the next adapter would ask for another one.
- **One switch for the model and the embedder:** simpler, but the free vectors would wait for the paid cards.
- **Pieces measured in tokens:** more exact, but it needs the tokenizer of the provider (another dependency and a download), and the pieces would change little.
- **Pieces of a fixed length that overlap** (10 to 20 % is common in retrieval for question answering): an overlap keeps a sentence that a cut splits. Our cuts are at the end of a paragraph or a sentence, so a cut seldom splits a sentence, and search ranks whole items, not passages.
