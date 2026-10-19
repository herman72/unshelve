import type { APIRequestContext } from "@playwright/test";

// Ports far from those of the stack (3000, 8000), so the tests can run next to it.
export const API_PORT = 3901;
export const WEB_PORT = 3900;
export const API_URL = `http://127.0.0.1:${API_PORT}`;

/** Put the made-up items of the pretend API back, as each test expects them. */
export async function resetApi(request: APIRequestContext): Promise<void> {
  await request.post(`${API_URL}/__reset`);
}

/** Change an item in the pretend API, as the worker would while a page is open. */
export async function changeItem(
  request: APIRequestContext,
  id: number,
  fields: Record<string, unknown>,
): Promise<void> {
  await request.post(`${API_URL}/__items/${id}`, { data: fields });
}

/** The ids of the items that the pretend API was asked to send back to the start. */
export async function reprocessed(request: APIRequestContext): Promise<number[]> {
  const answer = await request.get(`${API_URL}/__calls`);
  return ((await answer.json()) as { reprocessed: number[] }).reprocessed;
}

/** The moves that the pretend API was asked for, in order: the id of the item and its target. */
export async function moves(request: APIRequestContext): Promise<{ id: number; to: string }[]> {
  const answer = await request.get(`${API_URL}/__calls`);
  return ((await answer.json()) as { moves: { id: number; to: string }[] }).moves;
}

/** Change a goal in the pretend API, as another tab would while a page is open. */
export async function changeGoal(
  request: APIRequestContext,
  id: number,
  fields: Record<string, unknown>,
): Promise<void> {
  await request.post(`${API_URL}/__goals/${id}`, { data: fields });
}

type Verdict = { item: number; goal: number; label: string | null };

/** The verdicts on scores that the pretend API was given, in order. */
export async function verdicts(request: APIRequestContext): Promise<Verdict[]> {
  const answer = await request.get(`${API_URL}/__calls`);
  return ((await answer.json()) as { verdicts: Verdict[] }).verdicts;
}

/** The reading time of each preview of a digest that the pretend API was asked for, in order. */
export async function digestMinutes(request: APIRequestContext): Promise<number[]> {
  const answer = await request.get(`${API_URL}/__calls`);
  return ((await answer.json()) as { digestMinutes: number[] }).digestMinutes;
}

/** For the next previews: the digest has nothing to bring back. */
export async function emptyDigest(request: APIRequestContext): Promise<void> {
  await request.post(`${API_URL}/__digest`, { data: { empty: true } });
}

/** An event of an item in the pretend API, as the worker or the bot would write it. */
export async function addEvent(
  request: APIRequestContext,
  itemId: number,
  event: { type: string; actor?: string; payload?: Record<string, unknown> },
): Promise<void> {
  await request.post(`${API_URL}/__events/${itemId}`, { data: event });
}
