import { ItemCard } from "@/components/item-card";
import { type DigestPreview, REASONS } from "@/lib/digest";

/** The items of a digest, in the order of the digest, each with the reason that chose it. */
export function DigestPicks({ preview, minutes }: { preview: DigestPreview; minutes: number }) {
  const count = preview.picks.length;
  if (count === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-muted">
        Nothing to bring back now. An item comes back when it helps a goal, when you saved it
        again, or when it is new. A digest does not show an item again within three weeks.
      </p>
    );
  }
  return (
    <>
      <p className="mb-4 text-sm text-muted">
        {count === 1 ? "1 item" : `${count} items`}, about {preview.total_minutes} of {minutes}{" "}
        minutes.
        {preview.goal_title && (
          <>
            {" "}
            Focus goal: <bdi>{preview.goal_title}</bdi>.
          </>
        )}
      </p>
      {/* The order is the order of the digest: the focus goal first, the random pick last. */}
      <ol role="list" aria-label="Digest" className="flex flex-col gap-5">
        {preview.picks.map((pick) => (
          <li key={pick.item.id}>
            <p className="mb-1.5 text-sm font-medium">
              {REASONS[pick.reason]}
              {/* A goal title can be Persian: <bdi> keeps it from turning the line around. */}
              {pick.goal_title && (
                <>
                  : <bdi>{pick.goal_title}</bdi>
                </>
              )}
            </p>
            <ItemCard item={pick.item} />
          </li>
        ))}
      </ol>
    </>
  );
}
