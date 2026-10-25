"use client";

import Link from "next/link";
import { Fragment, useEffect, useRef, useState } from "react";
import { displayTitle } from "@/components/item-card";
import { VectorNote } from "@/components/vector-note";
import { browserApi } from "@/lib/browser-api";
import type { ItemStatus } from "@/lib/reading-status";
import { type Overview, numberSources } from "@/lib/search";

// How many of the best items the model reads: the default of the API. More items cost more.
const ITEMS_TO_READ = 12;

type PanelState =
  | { kind: "idle" }
  | { kind: "writing" }
  | { kind: "answer"; overview: Overview }
  | { kind: "failed"; message: string };

/** A detail of the API as a sentence: a capital letter first, and a full stop at the end. */
function sentence(text: string): string {
  const trimmed = text.trim();
  const end = /[.!?]$/.test(trimmed) ? "" : ".";
  return `${trimmed.charAt(0).toUpperCase()}${trimmed.slice(1)}${end}`;
}

/** What the API answered, in words for a person. */
function messageFor(status: number, detail: string | null | undefined): string {
  switch (status) {
    case 503:
      // The search works, the model does not: no key, or the daily budget is used. The API
      // says which.
      return `${sentence(detail ?? "the model gave no answer")} The results below do not need it.`;
    case 401:
      return "You are signed out. Send /login to your Unshelve bot to get a sign-in link.";
    default:
      return `The API answered with status ${status}. Try again in a moment.`;
  }
}

/**
 * The answer: the text of the model, with the number of each source after the statement that it
 * supports, then the sources with the quotes. Each piece is plain text, written as text: the
 * model read saved web pages, so its words are never HTML or Markdown here.
 */
function OverviewAnswer({ overview }: { overview: Overview }) {
  if (overview.segments.length === 0) {
    return <p className="mt-2">None of your saved items could be read for this question.</p>;
  }
  const { sources, cites } = numberSources(overview);

  return (
    <>
      <VectorNote state={overview.vector_search} />
      {/* whitespace-pre-line keeps the paragraphs of the model, which come as line breaks. */}
      <p dir="auto" className="mt-2 whitespace-pre-line">
        {overview.segments.map((segment, index) => (
          <Fragment key={index}>
            {segment.text}
            {(cites[index] ?? []).map((number) => (
              // dir="ltr" makes the number a unit of its own. Without it, the digits after a
              // Persian sentence join the run of its letters, and [2] shows on the far side of
              // the sentence that it belongs to.
              <sup key={number} dir="ltr" className="ms-0.5">
                <a
                  href={`#source-${number}`}
                  aria-label={`Source ${number}`}
                  className="text-accent hover:underline"
                >
                  [{number}]
                </a>
              </sup>
            ))}
          </Fragment>
        ))}
      </p>

      <h3 className="mt-4 text-sm font-semibold">Sources</h3>
      <ol role="list" className="mt-2 flex flex-col gap-3 text-sm">
        {sources.map((source) => (
          // scroll-mt keeps the source below the top edge when a number jumps to it.
          <li key={source.itemId} id={`source-${source.number}`} className="scroll-mt-4">
            <span className="text-muted">[{source.number}]</span>{" "}
            <Link
              href={`/items/${source.itemId}`}
              dir="auto"
              className="font-medium hover:underline"
            >
              {source.item ? displayTitle(source.item) : `Item ${source.itemId}`}
            </Link>
            {source.quotes.map((quote) => (
              <blockquote
                key={quote}
                dir="auto"
                className="mt-1 border-s-2 border-border ps-3 text-muted wrap-anywhere"
              >
                {quote}
              </blockquote>
            ))}
          </li>
        ))}
      </ol>
    </>
  );
}

/**
 * An answer to the search, written by the model from the saved items only, with its sources.
 *
 * It asks the model, which costs money, so it runs only when the person asks. The same question
 * on the same items costs nothing the second time: the API answers it from its ledger. A new
 * search mounts the results again (their key is the search), and the panel starts again with them.
 */
export function OverviewPanel({
  q,
  status,
  unread,
  narrowed,
}: {
  q: string;
  status: ItemStatus[];
  unread: boolean;
  narrowed: boolean;
}) {
  const [state, setState] = useState<PanelState>({ kind: "idle" });
  const answerRef = useRef<HTMLDivElement>(null);

  // The button disappears with the answer. Focus moves to the answer, so a keyboard or a screen
  // reader goes on from there and not from the top of the page.
  useEffect(() => {
    if (state.kind === "answer") answerRef.current?.focus();
  }, [state.kind]);

  async function write() {
    setState({ kind: "writing" });
    try {
      const { data, error, response } = await browserApi.POST("/api/v1/search/overview", {
        body: { q, status: status.length > 0 ? status : null, unread, limit: ITEMS_TO_READ },
      });
      setState(
        data
          ? { kind: "answer", overview: data }
          : { kind: "failed", message: messageFor(response.status, error?.detail) },
      );
    } catch {
      setState({
        kind: "failed",
        message: "Cannot reach the API. Check that it runs, then try again.",
      });
    }
  }

  return (
    <section aria-labelledby="overview-title" className="mb-8 rounded-lg border border-border p-4">
      <h2 id="overview-title" className="text-base font-semibold">
        Overview
      </h2>
      {state.kind === "answer" ? (
        <div ref={answerRef} tabIndex={-1} className="focus-visible:outline-none">
          <OverviewAnswer overview={state.overview} />
        </div>
      ) : (
        <>
          <p className="mt-1 text-sm text-muted">
            An answer to your search, written by the model from your saved items only. Each
            statement names its sources. It uses the model, so it is written only when you ask.
          </p>
          {narrowed && (
            <p className="mt-1 text-sm text-muted">
              It reads items of every kind and goal: of your filters, only the status and
              “Unread only” apply.
            </p>
          )}
          <button
            type="button"
            onClick={write}
            disabled={state.kind === "writing"}
            className="mt-3 rounded-md border border-muted px-3 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-60"
          >
            {state.kind === "failed" ? "Try again" : "Write an overview"}
          </button>
        </>
      )}
      {/* One live region for each change: screen readers announce what it says. */}
      <p
        role="status"
        className={state.kind === "answer" ? "sr-only" : "mt-2 text-sm text-muted"}
      >
        {state.kind === "writing" && "Writing the overview…"}
        {state.kind === "failed" && state.message}
        {state.kind === "answer" && "The overview is ready."}
      </p>
    </section>
  );
}
