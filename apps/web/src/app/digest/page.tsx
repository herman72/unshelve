import type { Metadata } from "next";
import Form from "next/form";
import { Suspense } from "react";
import { DigestPicks } from "@/components/digest-picks";
import { EmptyState } from "@/components/empty-state";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { sessionApi } from "@/lib/api";
import { toApiState } from "@/lib/api-state";
import { BUDGETS, budgetFrom, DEFAULT_BUDGET } from "@/lib/digest";

export const metadata: Metadata = { title: "Digest" };

type DigestProps = Pick<PageProps<"/digest">, "searchParams">;

export default function DigestPage({ searchParams }: DigestProps) {
  return (
    <>
      <PageHeader
        title="Digest"
        description="What a digest would bring back now, and why. This page sends nothing."
      />
      {/* The reading time is part of the URL, which is request data: it waits behind this boundary. */}
      <Suspense fallback={<BudgetForm />}>
        <DigestPanel searchParams={searchParams} />
      </Suspense>
    </>
  );
}

function BudgetForm({ minutes = DEFAULT_BUDGET }: { minutes?: number }) {
  return (
    // Form submits with GET and navigates in the browser, and it still works without JavaScript.
    <Form action="/digest" className="mb-6 flex flex-wrap items-center gap-2">
      <label htmlFor="minutes" className="text-sm font-medium">
        Time to read
      </label>
      <select
        id="minutes"
        name="minutes"
        defaultValue={minutes}
        className="rounded-md border border-muted bg-background px-3 py-2"
      >
        {BUDGETS.map((budget) => (
          <option key={budget} value={budget}>
            {budget < 60 ? `${budget} minutes` : budget === 60 ? "1 hour" : `${budget / 60} hours`}
          </option>
        ))}
      </select>
      <button
        type="submit"
        className="rounded-md bg-foreground px-4 py-2 font-medium text-background hover:opacity-90"
      >
        Show
      </button>
    </Form>
  );
}

async function DigestPanel({ searchParams }: DigestProps) {
  const minutes = budgetFrom((await searchParams).minutes);
  return (
    <>
      {/* The key resets the choice when the URL changes, for example after the Back button. */}
      <BudgetForm key={minutes} minutes={minutes} />
      <Suspense fallback={<Loading label="Choosing the items…" />}>
        <Preview minutes={minutes} />
      </Suspense>
    </>
  );
}

async function Preview({ minutes }: { minutes: number }) {
  const api = await sessionApi();
  const state = await toApiState(api.POST("/api/v1/digests/preview", { body: { minutes } }));
  if (state.kind !== "data") return <EmptyState state={state} />;
  return <DigestPicks preview={state.data} minutes={minutes} />;
}
