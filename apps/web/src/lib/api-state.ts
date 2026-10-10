/**
 * What a page can show for one API call.
 *
 * The API is young, so most calls answer 401 or 501 for now. Those are not failures of the
 * page. Each one is a state with its own short message (see components/empty-state.tsx).
 */
export type ApiFailure =
  | { kind: "signed-out" } // 401: no valid session
  | { kind: "not-built" } // 501: the endpoint is in the contract, its behaviour is not
  | { kind: "unreachable" } // the request got no answer at all
  | { kind: "error"; status: number }; // any other answer that carries no data

export type ApiState<T> = { kind: "data"; data: T } | ApiFailure;

/** What a typed API call resolves to: the parsed body on success, the response always. */
type Settled<T> = { data?: T; response: Response };

/**
 * Turn the result of a typed API call into a state a page can render.
 *
 * Keep this out of the component: ESLint does not allow JSX inside try/catch, so the call
 * is made and caught here, and the component only reads the state.
 */
export async function toApiState<T>(call: PromiseLike<Settled<T>>): Promise<ApiState<T>> {
  let settled: Settled<T>;
  try {
    settled = await call;
  } catch {
    return { kind: "unreachable" };
  }

  const { data, response } = settled;
  if (response.ok && data !== undefined) return { kind: "data", data };
  if (response.status === 401) return { kind: "signed-out" };
  if (response.status === 501) return { kind: "not-built" };
  return { kind: "error", status: response.status };
}
