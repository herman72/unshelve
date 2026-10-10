"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Inbox" },
  { href: "/queue", label: "Queue" },
  { href: "/search", label: "Search" },
  { href: "/goals", label: "Goals" },
] as const;

function isActive(pathname: string, href: string): boolean {
  // "/" is the parent of every path, so it matches only itself.
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * The main navigation. It is a client component because only the browser side of the router
 * knows the current path. `aria-current` is the one source of truth: screen readers announce
 * it, and the styles below read the same attribute.
 */
export function MainNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main">
      <ul role="list" className="flex flex-wrap gap-1">
        {LINKS.map(({ href, label }) => (
          <li key={href}>
            <Link
              href={href}
              aria-current={isActive(pathname, href) ? "page" : undefined}
              className="block rounded-md px-3 py-2 text-sm text-muted hover:text-foreground aria-[current=page]:font-semibold aria-[current=page]:text-foreground aria-[current=page]:underline aria-[current=page]:underline-offset-4"
            >
              {label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
