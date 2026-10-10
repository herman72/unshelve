import type { Metadata } from "next";
import Form from "next/form";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { ItemList } from "@/components/item-list";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";

export const metadata: Metadata = { title: "Search" };

type SearchProps = Pick<PageProps<"/search">, "searchParams">;

export default function SearchPage({ searchParams }: SearchProps) {
  return (
    <>
      <PageHeader
        title="Search"
        description="Find saved items by what they say, not only by their title."
      />
      {/* The query is part of the URL, which is request data. Whatever reads it waits behind this boundary, and the empty box is the static fallback. */}
      <Suspense fallback={<SearchBox />}>
        <SearchPanel searchParams={searchParams} />
      </Suspense>
    </>
  );
}

function SearchBox({ query = "" }: { query?: string }) {
  return (
    // Form submits with GET and navigates in the browser, and it still works without JavaScript.
    <Form action="/search" role="search" className="mb-6 flex gap-2">
      <label htmlFor="q" className="sr-only">
        Search your saved items
      </label>
      <input
        id="q"
        name="q"
        type="search"
        defaultValue={query}
        placeholder="Search your saved items"
        required
        maxLength={500}
        // The query can be English or Persian. 500 is the longest query the API accepts.
        dir="auto"
        className="min-w-0 flex-1 rounded-md border border-muted bg-background px-3 py-2 placeholder:text-muted"
      />
      <button
        type="submit"
        className="rounded-md bg-foreground px-4 py-2 font-medium text-background hover:opacity-90"
      >
        Search
      </button>
    </Form>
  );
}

/** The first value of a query parameter, which can be missing or repeated. */
function firstValue(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value) ?? "";
}

async function SearchPanel({ searchParams }: SearchProps) {
  const query = firstValue((await searchParams).q).trim();
  return (
    <>
      {/* The key resets the box when the query changes, for example after the Back button. */}
      <SearchBox key={query} query={query} />
      {query ? (
        <Suspense fallback={<Loading label="Searching…" />}>
          <SearchResults query={query} />
        </Suspense>
      ) : (
        <p className="text-muted">Type a few words to search the items you saved.</p>
      )}
    </>
  );
}

async function SearchResults({ query }: { query: string }) {
  const api = await sessionApi();
  const state = await toApiState(api.GET("/api/v1/search", { params: { query: { q: query } } }));
  if (state.kind !== "data") return <EmptyState state={state} />;
  return (
    <ItemList
      items={state.data.items.map((hit) => hit.item)}
      emptyMessage="Nothing you saved matches that search. Try other words."
    />
  );
}
