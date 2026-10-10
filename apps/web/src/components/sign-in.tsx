"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { browserApi } from "@/lib/browser-api";

type Outcome =
  | "checking"
  | "signed-in"
  | "no-link" // the page was opened without a token
  | "unavailable" // 501: the API cannot sign anyone in yet
  | "invalid" // 401 or 422: the token is wrong, expired or already used
  | "unreachable" // no answer from the API
  | "failed"; // any other answer

const MESSAGES: Record<Outcome, string> = {
  checking: "Checking your sign-in link…",
  "signed-in": "You are signed in.",
  "no-link": "To sign in, send /login to your Unshelve bot, then open the link it sends back.",
  unavailable: "Sign-in is not available yet.",
  invalid: "This link is not valid or has expired. Send /login to the bot again.",
  unreachable: "Cannot reach the API. Check that it is running, then open the link again.",
  failed:
    "Something went wrong while signing in. Open the link again, or send /login to the bot for a new one.",
};

/**
 * The token of a URL fragment shaped like `#token=...`, or null.
 *
 * It is read by hand and not with URLSearchParams, which turns a "+" into a space and would
 * change a token that contains one.
 */
function tokenFrom(hash: string): string | null {
  for (const part of hash.replace(/^#/, "").split("&")) {
    if (!part.startsWith("token=")) continue;
    const raw = part.slice("token=".length);
    try {
      return decodeURIComponent(raw) || null;
    } catch {
      return raw || null; // a broken % escape: send it as it is, and the API will refuse it
    }
  }
  return null;
}

/**
 * Take the token from the address, trade it for a session, and say how that went.
 *
 * The token is secret and works once. It is never logged, stored in state or shown: it goes
 * from the address into one request, and only the outcome comes back out.
 */
async function signIn(): Promise<Outcome> {
  const { hash, pathname, search } = window.location;
  const token = tokenFrom(hash);

  if (hash) {
    // Wait one tick before editing the address. Next.js wraps history.replaceState in an effect
    // of its router, which runs after this one. The wrapper keeps the router's own record of
    // the address in step and keeps its history state, so Back still works afterwards.
    await Promise.resolve();
    window.history.replaceState(null, "", pathname + search);
  }
  if (token === null) return "no-link";

  try {
    const { response } = await browserApi.POST("/api/v1/auth/magic/consume", { body: { token } });
    if (response.ok) return "signed-in";
    if (response.status === 501) return "unavailable";
    if (response.status === 401 || response.status === 422) return "invalid";
    return "failed";
  } catch {
    return "unreachable";
  }
}

export function SignIn() {
  const [outcome, setOutcome] = useState<Outcome>("checking");
  // React runs an effect twice in development. The link works once and the address can be
  // read once, so the attempt is made once, and both runs wait for the same answer.
  const attempt = useRef<Promise<Outcome> | null>(null);

  useEffect(() => {
    attempt.current ??= signIn();
    let active = true;
    void attempt.current.then((result) => {
      if (active) setOutcome(result);
    });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div role="status" className="rounded-lg border border-border bg-surface p-6">
      <p className="font-medium">{MESSAGES[outcome]}</p>
      {outcome === "signed-in" && (
        <p className="mt-3">
          <Link href="/" className="font-medium text-accent underline underline-offset-4">
            Go to your inbox
          </Link>
        </p>
      )}
    </div>
  );
}
