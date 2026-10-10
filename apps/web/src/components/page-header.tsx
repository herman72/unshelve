/** The title of a page and one sentence about it. */
export function PageHeader({ title, description }: { title: string; description: string }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="mt-1 text-muted">{description}</p>
    </header>
  );
}
