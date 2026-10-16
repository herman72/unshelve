# 0014. Scores: a vector prefilter, then one question to the model for all goals

- Status: Accepted
- Date: 2026-10-16

## Context

Each saved item gets a score from 0 to 3 for each goal of its owner, and a reason. The digest uses the scores to bring items back, and the owner confirms or rejects them later (M3-3): the labels become the evaluation set.

Four facts shaped the decision:

- **Most saved items fit no goal.** A channel of saved posts holds news, jokes and tools next to the few posts that serve a plan. A question to the model for each of them is paid for nothing.
- **Each item has a card vector already** (the embed step, [ADR 0012](0012-voyage-vectors-through-a-client-for-fixed-services.md)). A vector of each goal costs one call to the embedder for each version of the goal, and Voyage gives the first 200 million tokens free.
- **The ledger is the cache** ([ADR 0011](0011-claude-for-the-cards.md)). The same question has the same cache key, and the second time costs nothing.
- **Enrichment never sees goal text** ([ADR 0004](0004-public-data-is-a-pushed-static-subset.md)): a public summary must not quote the owner's plan. So the scores need a question of their own.

## Decision

- **A new step, `score_item`, after the embed step.** The embed step adds its job in the transaction that makes the item ready. A ready item that only gets the vectors of a new embedder keeps its scores. A merged copy gets no score of its own.
- **First the prefilter.** Each goal gets a vector of its title, description and keywords, as a query, in the embedding set of the embedder (table `goal_vectors`, one row for each goal and set, with the version of the goal). Postgres computes the cosine similarity of the card vector to each goal vector. When the closest goal is below `UNSHELVE_SCORE_PREFILTER` (0.3 by default), each goal gets 0, with the reason "The card is far from each goal", and the model is not asked. An item without a card vector, or a goal without a vector of its current version, skips the prefilter: nothing says that the item is far.
- **Then one question for all active goals.** The question lists each goal with a key (`g1`, `g2`, …) and the card of the item between `<item>` tags. The answer must fit a schema with one required field for each key: a score of 0, 1, 2 or 3, and a reason. So the model cannot leave a goal out. The prompt has a version (`score-1`).
- **Each score keeps where it came from:** `goal_version` (the version of the goal that was read) and `scorer_version` (`score-1:<model>`, or `score-1:prefilter`).
- **A score with a label stays as it is.** The label is the owner's verdict on the score that was shown. The write of a new score skips a row with a label, also when the label came during the question.
- **Old items are scored again by hand:** `unshelve goals rescore` finds the items without a score of the current goals, shows how many the prefilter answers, how many questions the ledger answers, and the price of the rest, and starts the jobs after a yes. A new goal does not start hundreds of paid questions by itself.

## Consequences

- The live check of 16 October (3 made-up cards, 2 goals, Claude Haiku 5.5) measured 933 to 965 input and 96 to 118 output tokens for each question: $0.00014 to $0.00016. With 7 goals, the estimate of the command is about $0.00045 for each item with Claude Haiku 5.5, and $0.018 with Claude Opus 5.5. The estimate counts 2.0 characters for each input token (2.1 measured: the schema of the answer adds tokens) and 60 output tokens for each goal (48 to 59 measured), so it stays a little above the price.
- The threshold of the prefilter is a cosine similarity of one embedder. In the same check, voyage-4-large gave 0.50 and 0.56 to the pairs of a card and the goal of its topic, and 0.11 to 0.23 to the pairs without a link. The first value, 0.2, would have sent the recipe to the model; 0.3 lies between the two groups. The owner's own items decide the final value: `unshelve goals rescore` shows how many items each value would answer, and it starts nothing without a yes.
- A false 0 of the prefilter hides a relevant item. The labels of M3-3 measure how often that happens.
- Each new item costs one question more, after its card. The daily budget of the ledger covers both.
- The question of the score holds the goal text. Its answer stays in the private table `item_goals`, which the public view does not read.

## Alternatives

- **One question for each goal.** Simpler answers, but N questions for each item, and the scores of one item are not comparable with each other.
- **Only the vectors, without the model.** Free and fast, but a similarity is no reason, and two texts that share words are not always useful for each other. The prefilter keeps the cheap part of this alternative.
- **Score again by itself after each change of a goal.** No command to remember, but a change of one word of a goal would start a paid question for each item.
