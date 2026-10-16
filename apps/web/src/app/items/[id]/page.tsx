import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { ItemView } from "@/components/item-detail";
import { Loading } from "@/components/loading";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";

// A fixed name: the title of the item is private data, read at request time.
export const metadata: Metadata = { title: "Item" };

// An id of the API: digits only, from 1. Number("1e3") would be 1000, so a pattern decides.
const ITEM_ID = /^[1-9][0-9]{0,15}$/;

export default function ItemPage({ params }: PageProps<"/items/[id]">) {
  return (
    // The id is URL data, and the item belongs to the visitor: both are read at request time,
    // inside the boundary, so the shell of the page can be sent at once.
    <Suspense fallback={<Loading label="Loading the item…" />}>
      <ItemDetails params={params} />
    </Suspense>
  );
}

async function ItemDetails({ params }: Pick<PageProps<"/items/[id]">, "params">) {
  const { id } = await params;
  if (!ITEM_ID.test(id)) notFound();
  const api = await sessionApi();
  const path = { item_id: Number(id) };
  // The two requests start at once: the history does not wait for the item.
  const [state, history] = await Promise.all([
    toApiState(api.GET("/api/v1/items/{item_id}", { params: { path } })),
    toApiState(api.GET("/api/v1/items/{item_id}/events", { params: { path } })),
  ]);
  if (state.kind === "error" && state.status === 404) notFound();
  if (state.kind !== "data") return <EmptyState state={state} />;
  return <ItemView item={state.data} history={history} />;
}
