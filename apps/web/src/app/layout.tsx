import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { Suspense } from "react";
import { ApiStatus, StatusLine } from "@/components/api-status";
import { MainNav, NavLinks } from "@/components/main-nav";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  // A page sets its own name, and the template adds the product name after it.
  title: { default: "Unshelve", template: "%s · Unshelve" },
  description: "A goal-aware reading inbox",
};

// The same column for the header, the page and the footer, with a 16px gutter on a phone.
const COLUMN = "mx-auto w-full max-w-3xl px-4 sm:px-6";

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-10 focus:rounded-md focus:bg-background focus:px-3 focus:py-2"
        >
          Skip to the content
        </a>
        <header className="border-b border-border">
          <div
            className={`${COLUMN} flex flex-wrap items-center justify-between gap-x-6 gap-y-1 py-3`}
          >
            <Link href="/" className="text-lg font-semibold tracking-tight">
              Unshelve
            </Link>
            {/* The path of a page like /items/1 is known only at request time. */}
            <Suspense fallback={<NavLinks pathname={null} />}>
              <MainNav />
            </Suspense>
          </div>
        </header>
        <main id="main" className={`${COLUMN} flex-1 py-8 sm:py-10`}>
          {children}
        </main>
        <footer className="border-t border-border">
          <div className={`${COLUMN} py-4`}>
            {/* The status is read at request time, so it streams in after the static shell. */}
            <Suspense fallback={<StatusLine ok={null}>Checking the API…</StatusLine>}>
              <ApiStatus />
            </Suspense>
          </div>
        </footer>
      </body>
    </html>
  );
}
