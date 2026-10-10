import { serverApi } from "@/lib/api";

type Status = { state: "ready"; version: string } | { state: "degraded" } | { state: "down" };

async function readStatus(): Promise<Status> {
  try {
    const { data } = await serverApi.GET("/api/v1/readyz");
    return data ? { state: "ready", version: data.version } : { state: "degraded" };
  } catch {
    return { state: "down" };
  }
}

export function StatusLine({ ok, children }: { ok: boolean | null; children: React.ReactNode }) {
  const dot = ok === null ? "bg-zinc-400" : ok ? "bg-emerald-500" : "bg-red-500";
  return (
    <p className="flex items-center gap-2 font-mono text-sm text-zinc-600 dark:text-zinc-400">
      <span aria-hidden className={`inline-block size-2 rounded-full ${dot}`} />
      {children}
    </p>
  );
}

export async function ApiStatus() {
  const status = await readStatus();
  if (status.state === "ready") {
    return <StatusLine ok>API ready, version {status.version}</StatusLine>;
  }
  if (status.state === "degraded") {
    return <StatusLine ok={false}>API is up, but its database is not reachable</StatusLine>;
  }
  return <StatusLine ok={false}>API is not reachable</StatusLine>;
}
