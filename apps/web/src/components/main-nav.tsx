"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Inbox" },
  { href: "/queue", label: "Queue" },
  { href: "/digest", label: "Digest" },
  { href: "/search", label: "Search" },
  { href: "/goals", label: "Goals" },
  { href: "/stats", label: "Stats" },
] as const;

function isActive(pathname: string | null, href: string): boolean {
  if (pathname === null) return false;
  // "/" is the parent of every path, so it matches only itself.
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * The links of the main navigation. `aria-current` is the one source of truth: screen readers
 * announce it, and the styles below read the same attribute. Without a path, no link is marked:
 * that is the fallback while the path is not known yet (see MainNav).
 */
export function NavLinks({ pathname }: { pathname: string | null }) {
  return (
    <nav aria-label="Main">
      {/* On a phone, the padding of the links is the only space: six links then fit on 360 px. */}
      <ul role="list" className="flex flex-wrap sm:gap-1">
        {LINKS.map(({ href, label }) => (
          <li key={href}>
            <Link
              href={href}
              aria-current={isActive(pathname, href) ? "page" : undefined}
              className="block rounded-md px-2 py-2 text-sm text-muted sm:px-3 hover:text-foreground aria-[current=page]:font-semibold aria-[current=page]:text-foreground aria-[current=page]:underline aria-[current=page]:underline-offset-4"
            >
              {label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/**
 * The main navigation, with the link of the current page marked. It is a client component
 * because only the browser side of the router knows the current path.
 *
 * On a page with an id in its path (/items/1), the path is not known when the build prerenders
 * the layout. Then usePathname() suspends, so the layout puts this in <Suspense>, with
 * <NavLinks pathname={null} /> as its fallback.
 */
export function MainNav() {
  return <NavLinks pathname={usePathname()} />;
}
