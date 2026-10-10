import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { GoalList } from "@/components/goal-list";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";

export const metadata: Metadata = { title: "Goals" };

export default function GoalsPage() {
  return (
    <>
      <PageHeader
        title="Goals"
        description="What you are working towards, which every saved item is scored against."
      />
      <Suspense fallback={<Loading label="Loading your goals…" />}>
        <Goals />
      </Suspense>
    </>
  );
}

async function Goals() {
  const api = await sessionApi();
  const state = await toApiState(api.GET("/api/v1/goals"));
  if (state.kind !== "data") return <EmptyState state={state} />;
  return <GoalList goals={state.data.items} />;
}
