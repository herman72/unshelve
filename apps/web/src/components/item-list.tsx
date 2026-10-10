import { ItemCard, type ItemSummary } from "@/components/item-card";

export function ItemList({
  items,
  emptyMessage = "Nothing here yet.",
}: {
  items: readonly ItemSummary[];
  emptyMessage?: string;
}) {
  if (items.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-muted">
        {emptyMessage}
      </p>
    );
  }
  return (
    // role="list" keeps the list semantics in Safari, which drops them when list-style is none.
    <ul role="list" className="flex flex-col gap-3">
      {items.map((item) => (
        <li key={item.id}>
          <ItemCard item={item} />
        </li>
      ))}
    </ul>
  );
}
