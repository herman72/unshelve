import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { ItemList } from "@/components/item-list";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";

export const metadata: Metadata = { title: "Queue" };

export default function QueuePage() {
  return (
    <>
      <PageHeader title="Queue" description="The few items you plan to read next." />
      <Suspense fallback={<Loading label="Loading your queue…" />}>
        <QueueItems />
      </Suspense>
    </>
  );
}

async function QueueItems() {
  const api = await sessionApi();
  const state = await toApiState(api.GET("/api/v1/queue"));
  if (state.kind !== "data") return <EmptyState state={state} />;

  const { limit, used, items } = state.data;
  return (
    <>
      <p className="mb-4 text-sm text-muted">
        {used} of {limit} queue slots used.
      </p>
      <ItemList
        items={items}
        emptyMessage="Your queue is empty. Move an item here from the inbox when you plan to read it."
      />
    </>
  );
}
