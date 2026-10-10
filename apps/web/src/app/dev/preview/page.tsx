import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ItemList } from "@/components/item-list";
import { PageHeader } from "@/components/page-header";
import { PREVIEW_ITEMS } from "./fixtures";

export const metadata: Metadata = { title: "Preview" };

export default function PreviewPage() {
  // A tool for building the item components. It shows fixtures, and it does not exist in production.
  if (process.env.NODE_ENV === "production") notFound();

  return (
    <>
      <PageHeader
        title="Preview"
        description="Fixture items for checking how the item components look, with no API."
      />
      <ItemList items={PREVIEW_ITEMS} />
    </>
  );
}
