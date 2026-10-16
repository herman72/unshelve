import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/empty-state";
import { GoalForm } from "@/components/goal-form";
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
      <section className="mt-10 rounded-lg border border-border p-4">
        <h2 className="text-base font-semibold">Add a goal</h2>
        <p className="mt-1 text-sm text-muted">
          Name one thing that you work on now. The description and the keywords tell the model
          what belongs to it.
        </p>
        <div className="mt-4">
          <GoalForm />
        </div>
      </section>
    </>
  );
}

async function Goals() {
  const api = await sessionApi();
  const state = await toApiState(
    api.GET("/api/v1/goals", { params: { query: { include_archived: true } } }),
  );
  if (state.kind !== "data") return <EmptyState state={state} />;
  return <GoalList goals={state.data.items} />;
}
