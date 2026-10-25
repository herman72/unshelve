import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Odds and ends",
  description: "Things that I read, watched and kept, with a search that runs in the browser.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
