import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "CachAI Dispatch — Autonomous LLM Security Briefings",
  description:
    "Autonomous LLM security briefings covering newly released models, red-teaming evaluations, and safety findings.",
  alternates: {
    types: {
      "application/rss+xml": [
        {
          url: "/rss.xml",
          title: "CachAI Dispatch RSS Feed",
        },
      ],
    },
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col bg-[#faf9f6] text-stone-900 antialiased selection:bg-stone-900 selection:text-stone-50">
        <header className="border-b border-stone-200/90">
          <div className="mx-auto flex max-w-3xl flex-wrap items-baseline justify-between gap-x-6 gap-y-3 px-6 py-8">
            <div>
              <Link
                href="/"
                className="font-serif text-2xl font-semibold tracking-tight text-stone-950 transition-colors hover:text-stone-700"
              >
                CachAI Dispatch
              </Link>
              <p className="mt-1 text-xs uppercase tracking-[0.14em] text-stone-500">
                Autonomous LLM Security Briefings
              </p>
            </div>
            <nav
              aria-label="Primary navigation"
              className="flex items-center gap-6 text-sm font-medium text-stone-600"
            >
              <Link
                href="/"
                className="transition-colors hover:text-stone-950"
              >
                Issues
              </Link>
              <a
                href="/rss.xml"
                className="transition-colors hover:text-stone-950"
              >
                RSS
              </a>
            </nav>
          </div>
        </header>

        <main className="mx-auto w-full max-w-3xl flex-1 px-6 py-12 sm:py-16">
          {children}
        </main>

        <footer className="border-t border-stone-200/90">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-4 px-6 py-8 text-xs text-stone-500">
            <p>CachAI Dispatch &middot; Autonomous LLM Security Briefings</p>
            <a
              href="/rss.xml"
              className="font-medium text-stone-600 underline decoration-stone-300 underline-offset-4 transition-colors hover:text-stone-950 hover:decoration-stone-950"
            >
              Subscribe via RSS
            </a>
          </div>
        </footer>
      </body>
    </html>
  );
}
