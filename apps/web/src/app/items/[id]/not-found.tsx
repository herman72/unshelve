import Link from "next/link";

/** What the page of an item shows when the API knows no such item for this visitor. */
export default function ItemNotFound() {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center">
      <p className="font-medium">No such item</p>
      <p className="mt-1 text-muted">
        It was never saved, or it is not yours.{" "}
        <Link href="/" className="text-accent hover:underline">
          Back to the inbox
        </Link>
      </p>
    </div>
  );
}
