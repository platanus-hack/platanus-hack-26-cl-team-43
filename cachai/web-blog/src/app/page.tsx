import Link from "next/link";
import { fetchPublishedPosts, type BlogPost } from "@/lib/api";

export const dynamic = "force-dynamic";

function formatIssueDate(iso: string | null): string {
  if (!iso) return "Unscheduled";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "Unscheduled";
  return parsed.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function estimateReadTimeMinutes(markdown: string): number {
  const words = markdown.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.ceil(words / 200));
}

function sortNewestFirst(posts: BlogPost[]): BlogPost[] {
  return [...posts].sort((a, b) => {
    const timeA = a.published_at ? Date.parse(a.published_at) : 0;
    const timeB = b.published_at ? Date.parse(b.published_at) : 0;
    const safeA = Number.isNaN(timeA) ? 0 : timeA;
    const safeB = Number.isNaN(timeB) ? 0 : timeB;
    if (safeB !== safeA) {
      return safeB - safeA;
    }
    return b.id - a.id;
  });
}

export default async function HomePage() {
  const posts = sortNewestFirst(await fetchPublishedPosts());

  if (posts.length === 0) {
    return (
      <section className="py-12 text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-stone-500">
          CachAI Dispatch
        </p>
        <h1 className="mt-3 font-serif text-2xl font-semibold tracking-tight text-stone-900">
          No issues published yet
        </h1>
        <p className="mx-auto mt-3 max-w-md text-sm leading-relaxed text-stone-600">
          Autonomous LLM security briefings will appear here as soon as the
          first evaluation issue is published.
        </p>
      </section>
    );
  }

  const [latest, ...archive] = posts;
  const latestReadTime = estimateReadTimeMinutes(latest.markdown_body);

  return (
    <div className="space-y-16">
      <section aria-labelledby="latest-issue-heading">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-stone-500">
          <span
            id="latest-issue-heading"
            className="font-semibold uppercase tracking-[0.16em] text-stone-900"
          >
            Latest Issue
          </span>
          <span aria-hidden="true">&middot;</span>
          <time dateTime={latest.published_at ?? undefined}>
            {formatIssueDate(latest.published_at)}
          </time>
          <span aria-hidden="true">&middot;</span>
          <span>{latestReadTime} min read</span>
        </div>

        <h1 className="mt-4 font-serif text-3xl font-semibold leading-tight tracking-tight text-stone-950 sm:text-4xl">
          <Link
            href={`/posts/${latest.slug}`}
            className="transition-colors hover:text-stone-600"
          >
            {latest.title}
          </Link>
        </h1>

        <p className="mt-4 text-base leading-relaxed text-stone-600 sm:text-lg">
          {latest.summary}
        </p>

        <div className="mt-6">
          <Link
            href={`/posts/${latest.slug}`}
            className="inline-flex items-center text-sm font-medium text-stone-900 underline decoration-stone-400 underline-offset-4 transition-colors hover:text-stone-600 hover:decoration-stone-900"
          >
            Read briefing &rarr;
          </Link>
        </div>
      </section>

      {archive.length > 0 && (
        <section
          aria-labelledby="archive-heading"
          className="border-t border-stone-200/90 pt-12"
        >
          <h2
            id="archive-heading"
            className="text-xs font-semibold uppercase tracking-[0.16em] text-stone-500"
          >
            Archive
          </h2>

          <ul className="mt-6 divide-y divide-stone-200/80">
            {archive.map((post) => {
              const readTime = estimateReadTimeMinutes(post.markdown_body);
              return (
                <li key={post.id} className="py-7 first:pt-2 last:pb-0">
                  <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs text-stone-500">
                    <time dateTime={post.published_at ?? undefined}>
                      {formatIssueDate(post.published_at)}
                    </time>
                    <span aria-hidden="true">&middot;</span>
                    <span>{readTime} min read</span>
                  </div>

                  <h3 className="mt-2 font-serif text-xl font-semibold leading-snug tracking-tight text-stone-950">
                    <Link
                      href={`/posts/${post.slug}`}
                      className="transition-colors hover:text-stone-600"
                    >
                      {post.title}
                    </Link>
                  </h3>

                  <p className="mt-2 text-sm leading-relaxed text-stone-600">
                    {post.summary}
                  </p>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
