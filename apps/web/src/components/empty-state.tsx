import type { ApiFailure } from "@/lib/api-state";

function describe(state: ApiFailure): { title: string; message: string } {
  switch (state.kind) {
    case "signed-out":
      return {
        title: "You are signed out",
        message: "Send /login to your Unshelve bot to get a sign-in link.",
      };
    case "not-built":
      return {
        title: "Not built yet",
        message: "This part of Unshelve is not ready. It will show up here when it is.",
      };
    case "unreachable":
      return {
        title: "Cannot reach the API",
        message: "Check that the API is running, then reload this page.",
      };
    case "error":
      return {
        title: "The API reported a problem",
        message: `It answered with status ${state.status}. Try again in a moment.`,
      };
  }
}

/** What a page shows when its API call returned no data. */
export function EmptyState({ state }: { state: ApiFailure }) {
  const { title, message } = describe(state);
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center">
      <p className="font-medium">{title}</p>
      <p className="mt-1 text-muted">{message}</p>
    </div>
  );
}
