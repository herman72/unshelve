import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { ItemList } from "@/components/item-list";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";

// The title template of the root layout does not reach the page of its own segment, so the
// product name is written out here. The other pages get it from the template.
export const metadata: Metadata = { title: "Inbox · Unshelve" };

export default function InboxPage() {
  return (
    <>
      <PageHeader title="Inbox" description="Everything you saved that you have not sorted yet." />
      {/* The list belongs to the visitor: it is read at request time and streams in after the shell. */}
      <Suspense fallback={<Loading label="Loading your inbox…" />}>
        <InboxItems />
      </Suspense>
    </>
  );
}

async function InboxItems() {
  const api = await sessionApi();
  // The inbox holds the items whose reading status is "inbox". Queued and finished items are elsewhere.
  const state = await toApiState(
    api.GET("/api/v1/items", { params: { query: { status: ["inbox"] } } }),
  );
  if (state.kind !== "data") return <EmptyState state={state} />;
  return (
    <ItemList
      items={state.data.items}
      emptyMessage="Nothing to sort. New items from your Telegram channel show up here."
    />
  );
}
