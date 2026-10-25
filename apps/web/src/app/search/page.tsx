import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { Loading } from "@/components/loading";
import { OverviewPanel } from "@/components/overview-panel";
import { PageHeader } from "@/components/page-header";
import { SearchForm } from "@/components/search-form";
import { SearchHitCard } from "@/components/search-hit";
import { VectorNote } from "@/components/vector-note";
import { sessionApi } from "@/lib/api";
import { type ApiState, toApiState } from "@/lib/api-state";
import {
  type Search,
  type SearchPage as Page,
  apiQuery,
  parseSearch,
  searchHref,
} from "@/lib/search";

export const metadata: Metadata = { title: "Search" };

type SearchProps = Pick<PageProps<"/search">, "searchParams">;

export default function SearchPage({ searchParams }: SearchProps) {
  return (
    <>
      <PageHeader
        title="Search"
        description="Find saved items by what they say, not only by their title."
      />
      {/* The search is part of the URL, which is request data. Whatever reads it waits behind
          this boundary, and the empty form is the static fallback. */}
      <Suspense fallback={<SearchForm />}>
        <SearchPanel searchParams={searchParams} />
      </Suspense>
    </>
  );
}

async function SearchPanel({ searchParams }: SearchProps) {
  const search = parseSearch(await searchParams);
  const api = await sessionApi();
  // Both requests start now. The form waits only for the goals; the results stream in after it.
  const results = search.q
    ? toApiState(api.GET("/api/v1/search", { params: { query: apiQuery(search) } }))
    : null;
  const goals = await toApiState(
    api.GET("/api/v1/goals", { params: { query: { include_archived: true } } }),
  );

  return (
    <>
      {/* No key here: a key equal to the key of the results next to it would make React keep
          the old form on the page (docs/learn/41). The form puts its key inside. */}
      <SearchForm search={search} goals={goals.kind === "data" ? goals.data.items : []} />
      {results ? (
        // The key is the search without its cursor. A new search shows the fallback at once,
        // and the overview starts again; the next page of the same search keeps both.
        <Suspense key={searchHref(search)} fallback={<Loading label="Searching…" />}>
          <SearchResults search={search} results={results} />
        </Suspense>
      ) : (
        <p className="text-muted">Type a few words to search the items you saved.</p>
      )}
    </>
  );
}

async function SearchResults({
  search,
  results,
}: {
  search: Search;
  results: Promise<ApiState<Page>>;
}) {
  const state = await results;
  if (state.kind !== "data") return <EmptyState state={state} />;
  const { items: hits, next_cursor: next, vector_search } = state.data;
  const filtered =
    search.status.length > 0 ||
    search.unread ||
    search.kind !== null ||
    search.goalId !== null ||
    search.tags.length > 0;

  if (hits.length === 0) {
    return (
      <>
        <VectorNote state={vector_search} />
        <p className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-muted">
          {search.cursor
            ? "No more results for this search."
            : `Nothing you saved matches that search. Try other words${
                filtered ? ", or fewer filters" : ""
              }.`}
        </p>
      </>
    );
  }

  return (
    <>
      <VectorNote state={vector_search} />
      <OverviewPanel
        q={search.q}
        status={search.status}
        unread={search.unread}
        narrowed={search.kind !== null || search.goalId !== null || search.tags.length > 0}
      />
      <section aria-labelledby="results-title">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="results-title" className="text-base font-semibold">
            {search.cursor ? "More results" : "Results"}
          </h2>
          {search.cursor && (
            <Link href={searchHref(search)} className="text-sm text-accent hover:underline">
              Back to the best results
            </Link>
          )}
        </div>
        {/* role="list" keeps the list semantics in Safari, which drops them without list-style. */}
        <ol role="list" className="flex flex-col gap-3">
          {hits.map((hit) => (
            <li key={hit.item.id}>
              <SearchHitCard hit={hit} search={search} />
            </li>
          ))}
        </ol>
        {/* The cursor is bound to this search: the API refuses it for other words or filters. */}
        {next && (
          <p className="mt-6 text-center">
            <Link href={searchHref(search, next)} className="text-accent hover:underline">
              More results
            </Link>
          </p>
        )}
      </section>
    </>
  );
}
