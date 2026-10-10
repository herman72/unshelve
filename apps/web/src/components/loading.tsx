/** The fallback of a <Suspense> boundary around data that is read at request time. */
export function Loading({ label }: { label: string }) {
  return (
    <p role="status" className="py-10 text-center text-muted">
      {label}
    </p>
  );
}
