# 6. The API contract and the web shell

> Pull request: [#49](https://github.com/herman72/unshelve/pull/49) · Code: [`api/schemas.py`](../../apps/api/src/unshelve/api/schemas.py), [`api/routes/`](../../apps/api/src/unshelve/api/routes/), [`tests/api/`](../../apps/api/tests/api/), [`apps/web/src/`](../../apps/web/src/)

## In one minute

The API is the door between the web app and the data.

This step writes down every endpoint of version 1. There are 27. Each one has its inputs and its outputs, but not its real behaviour yet. The web app gets its pages, its navigation and its types from this plan.

Every endpoint is locked by default.

## 1. What a stub looks like

```python
@router.get("/items", responses=problems(401, 422))
async def list_items(query: Annotated[ItemListQuery, Query()]) -> ItemPage:
    """A page of items, filtered and sorted by the query."""
    raise NotImplementedYetError
```

The inputs and the output have types. Only the body is missing. This is what you get today:

```text
$ curl -i http://localhost:8000/api/v1/items
HTTP/1.1 401 Unauthorized
content-type: application/problem+json

{"type":"about:blank","title":"Unauthorized","status":401,"detail":"sign in first"}
```

## 2. "Deny by default"

**A comparison from daily life:** a hotel. Every room is locked. Only the rooms on a short list (the lobby) are open.

In this API the short list has three names:

```python
PUBLIC_OPERATIONS = {"healthz", "readyz", "consume_magic_link"}
```

Imagine that somebody adds a new endpoint and forgets the protection:

| | Each endpoint must add its own protection | Deny by default (this step) |
|---|---|---|
| New endpoint, protection forgotten | Open to everybody. Nobody sees it. | A test fails. You must decide, and the reviewer sees your decision. |

A request without a session gets 401. A request with a made-up cookie gets 501, because sessions do not exist yet. It is never accepted.

## 3. One format for every error

Every error from this API has the same shape. Example: a request without the required field `token`.

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "errors": [{ "pointer": "/body/token", "detail": "Field required" }]
}
```

Why: the web app needs only one piece of code to show any error. The format is a public standard with the name "problem details" (RFC 9457).

## 4. The list of all endpoints is a test

`test_surface.py` contains the list of all endpoints: the method, the path and the name. If an endpoint appears or disappears, this test fails. So the API cannot change by accident. It is also the fastest way for you to see everything that the product can do.

## 5. Two mistakes in the first version, and how they were found

A second person had to *use* the error code. That person found two mistakes that the author's own tests did not find.

| Mistake | Example | Correction |
|---|---|---|
| A "wrong method" answer did not say which methods are allowed | `DELETE /healthz` → 405, but no `Allow: GET` header | The header is sent now. A test checks it. |
| The error titles came from Python, and Python changed them | Python 3.12: "Unprocessable Entity". Python 3.13: "Unprocessable Content". | The titles are written in our code now. |

The second mistake is important. The titles are also in `openapi.json`. So the same code gave a different file on two machines.

**The lesson:** the first user of your code finds what you cannot see. That is the reason for code review.

## A choice that we made

When do we write an endpoint? There are two ways.

1. **Now, as a stub.** A stub is an endpoint that exists but answers "not built yet". This step does that.
2. **Later**, when its real code is written.

**A comparison from daily life:** the plan of a house. All rooms are drawn before one wall is built. The electrician and the plumber can plan at the same time, because both read the same plan.

Here the "plan" is `openapi.json`. The web app reads it today and knows the shape of each answer. The real code for each endpoint comes later, and the web app does not need to change.

## What a senior engineer checks

- **Can the API change by accident?** No: see part 4.
- **Does an error show secret text?** `test_problem_details.py` raises an error with the text "secret text" and checks that the answer does not contain it.
- **Does the same input give the same file on every machine?** See part 5.

## Exercise (15 minutes)

1. Add a stub endpoint `GET /api/v1/items/{item_id}/captures` in `api/routes/items.py`. Copy a stub that is near it.
2. Run `cd apps/api && uv run pytest tests/api`.
3. Count the tests that fail. For each one, write one sentence: what does this test protect?

## Words used in this note

| Word | Meaning |
|---|---|
| Endpoint | One address of the API together with a method, for example `GET /api/v1/items` |
| Stub | An endpoint that exists but answers "not built yet" |
| OpenAPI | A standard file format that describes an API |
| Status code | The number in an answer that says what happened: 200 OK, 401 not signed in, 404 not found, 422 wrong input |
| Session | The proof that you are signed in. The browser keeps it in a cookie. |
| Deny by default | Everything is forbidden, except the things on a short list |

## To read more

- [RFC 9457, Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457.html). Read section 3, "The Problem Details JSON Object".
- [FastAPI, "Bigger Applications"](https://fastapi.tiangolo.com/tutorial/bigger-applications/). It explains routers and shared dependencies.
- [OWASP Top 10, "Broken Access Control"](https://owasp.org/Top10/A01_2021-Broken_Access_Control/). It is the most common type of security problem in web applications. Read "How to Prevent".

Previous: [5. The worker loop](05-worker-loop.md) · Next: [7. Stacked pull requests with git](07-stacked-pull-requests.md)
