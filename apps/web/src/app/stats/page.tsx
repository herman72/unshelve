import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { StatsView } from "@/components/stats-view";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";

export const metadata: Metadata = { title: "Stats" };

export default function StatsPage() {
  return (
    <>
      <PageHeader
        title="Stats"
        description="The job queue, what the model costs, and your items, at one moment."
      />
      <Suspense fallback={<Loading label="Counting…" />}>
        <StatsPanel />
      </Suspense>
    </>
  );
}

async function StatsPanel() {
  const api = await sessionApi();
  const state = await toApiState(api.GET("/api/v1/stats"));
  if (state.kind !== "data") return <EmptyState state={state} />;
  return <StatsView stats={state.data} />;
}
